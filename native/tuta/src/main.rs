//! Experimental native Tuta adapter. One JSON request/response on private stdio.
//! No IMAP, SMTP, listener, keyring, database, or message logging.
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{self, Read},
    sync::{Arc, Mutex},
    time::Duration,
};
use tutabridge_tuta::mail::MailExtensions;
use tutasdk::{
    bindings::{
        file_client::{FileClient, FileClientError},
        rest_client::RestClient,
    },
    entities::generated::tutanota::{Mail, MailSet, MailSetEntry},
    login::{CredentialType, Credentials},
    net::native_rest_client::NativeRestClient,
    GeneratedId, IdTupleGenerated, ListLoadDirection, LoggedInSdk, Sdk,
};

const VERSION: u32 = 1;
const MAX_REQUEST_BYTES: u64 = 1024 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(90);
const TUTA_URL: &str = "https://app.tuta.com";

type Result<T> = std::result::Result<T, &'static str>;

// Deliberately not Debug: this structure contains session and wrapping keys.
#[derive(Deserialize, Serialize)]
struct Session {
    login: String,
    user_id: String,
    access_token: String,
    encrypted_passphrase_key: Vec<u8>,
}
impl Session {
    fn credentials(self) -> Credentials {
        Credentials {
            login: self.login,
            user_id: GeneratedId(self.user_id),
            access_token: self.access_token,
            encrypted_passphrase_key: self.encrypted_passphrase_key,
            credential_type: CredentialType::Internal,
        }
    }
    fn from_credentials(c: Credentials) -> Self {
        Self {
            login: c.login,
            user_id: c.user_id.to_string(),
            access_token: c.access_token,
            encrypted_passphrase_key: c.encrypted_passphrase_key,
        }
    }
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Operation {
    Login {
        email: String,
        password: String,
        totp: Option<String>,
    },
    Sync {
        session: Session,
        folder: Option<String>,
        limit: usize,
        offset: usize,
    },
    Message {
        session: Session,
        id: String,
    },
    MarkRead {
        session: Session,
        ids: Vec<String>,
        is_read: bool,
    },
    Move {
        session: Session,
        ids: Vec<String>,
        destination: String,
    },
}
#[derive(Deserialize)]
struct Request {
    version: u32,
    #[serde(flatten)]
    operation: Operation,
}

#[derive(Default)]
struct MemoryFiles(Mutex<HashMap<String, Vec<u8>>>);
#[async_trait::async_trait]
impl FileClient for MemoryFiles {
    async fn persist_content(
        &self,
        name: String,
        content: Vec<u8>,
    ) -> std::result::Result<(), FileClientError> {
        self.0
            .lock()
            .map_err(|_| FileClientError::Unknown)?
            .insert(name, content);
        Ok(())
    }
    async fn read_content(&self, name: String) -> std::result::Result<Vec<u8>, FileClientError> {
        self.0
            .lock()
            .map_err(|_| FileClientError::Unknown)?
            .get(&name)
            .cloned()
            .ok_or(FileClientError::NotFound)
    }
}

fn tuple(value: &str) -> Result<IdTupleGenerated> {
    let (list, element) = value.split_once('/').ok_or("invalid_id")?;
    if [list, element].iter().any(|p| {
        p.is_empty()
            || !p
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    }) {
        return Err("invalid_id");
    }
    Ok(IdTupleGenerated::new(
        GeneratedId(list.into()),
        GeneratedId(element.into()),
    ))
}
fn id(value: &IdTupleGenerated) -> String {
    format!("{}/{}", value.list_id, value.element_id)
}

fn role(kind: i64) -> (&'static str, &'static str) {
    match kind {
        1 => ("inbox", "Inbox"),
        2 => ("sent", "Sent"),
        3 => ("trash", "Trash"),
        4 => ("archive", "Archive"),
        5 => ("junk", "Spam"),
        6 => ("drafts", "Drafts"),
        _ => ("other", "Folder"),
    }
}
fn header(mail: &Mail) -> Result<Value> {
    if !mail._errors.is_empty() {
        return Err("decryption_failed");
    }
    let mail_id = id(mail._id.as_ref().ok_or("incomplete_response")?);
    Ok(
        json!({"id": mail_id, "subject": mail.subject, "date_ms": mail.receivedDate.as_millis(),
        "sender": {"name": mail.sender.name, "address": mail.sender.address},
        "recipient": mail.firstRecipient.as_ref().map(|r| json!({"name": r.name, "address": r.address})),
        "is_unread": mail.unread, "confidential": mail.confidential}),
    )
}
async fn resume(sdk: &Sdk, session: Session) -> Result<Arc<LoggedInSdk>> {
    // No automatic reauthentication: a refused client version is not an invalid password.
    sdk.login(session.credentials())
        .await
        .map_err(|_| "resume_failed")
}
async fn login(sdk: &Sdk, email: String, password: String, totp: Option<String>) -> Result<Value> {
    let initiated = sdk
        .initiate_session(&email, &password, "Postcard (experimental)")
        .await
        .map_err(|_| "login_failed")?;
    if !initiated.challenges.is_empty() {
        if !initiated.challenges.iter().any(|c| c.r#type == 1) {
            return Err("unsupported_second_factor");
        }
        let code = totp.ok_or("second_factor_required")?;
        if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
            return Err("invalid_totp");
        }
        sdk.authenticate_with_second_factor_totp(
            &initiated.credentials.access_token,
            code.parse().map_err(|_| "invalid_totp")?,
        )
        .await
        .map_err(|_| "second_factor_failed")?;
        for _ in 0..30 {
            if !sdk
                .is_second_factor_pending(&initiated.credentials.access_token)
                .await
                .map_err(|_| "second_factor_failed")?
            {
                break;
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    }
    sdk.login(initiated.credentials.clone())
        .await
        .map_err(|_| "login_failed")?;
    Ok(json!({"session": Session::from_credentials(initiated.credentials)}))
}

async fn sync(
    sdk: Arc<LoggedInSdk>,
    selected: Option<String>,
    limit: usize,
    offset: usize,
) -> Result<Value> {
    if limit == 0 || limit > 1000 || offset > 1_000_000 {
        return Err("invalid_page");
    }
    let facade = sdk.mail_facade();
    let client = facade.get_crypto_entity_client();
    let mailbox = facade
        .load_user_mailbox()
        .await
        .map_err(|_| "request_failed")?;
    let folders: Vec<MailSet> = client
        .load_all(&mailbox.mailSets.mailSets, ListLoadDirection::ASC)
        .await
        .map_err(|_| "request_failed")?;
    let mut folder_rows = Vec::new();
    let mut target: Option<MailSet> = None;
    for folder in folders {
        if !folder._errors.is_empty() {
            return Err("decryption_failed");
        }
        let folder_id = id(folder._id.as_ref().ok_or("incomplete_response")?);
        let (folder_role, fallback) = role(folder.folderType);
        folder_rows.push(json!({"id": folder_id, "role": folder_role,
            "label": if folder.name.is_empty() { fallback } else { &folder.name },
            "parent": folder.parentFolder.as_ref().map(id).unwrap_or_default()}));
        if selected.as_ref() == Some(&folder_id) || (selected.is_none() && folder.folderType == 1) {
            target = Some(folder);
        }
    }
    let target = target.ok_or("folder_not_found")?;
    // Full entry enumeration is authoritative only on success; callers must not prune on errors.
    let entries: Vec<MailSetEntry> = client
        .load_all(&target.entries, ListLoadDirection::DESC)
        .await
        .map_err(|_| "request_failed")?;
    let all_ids: Vec<String> = entries.iter().map(|e| id(&e.mail)).collect();
    let mut messages = Vec::new();
    for entry in entries.iter().skip(offset).take(limit) {
        let mail: Mail = client
            .load(&entry.mail)
            .await
            .map_err(|_| "request_failed")?;
        messages.push(header(&mail)?);
    }
    Ok(json!({"folders": folder_rows, "messages": messages,
        "folder": id(target._id.as_ref().ok_or("incomplete_response")?),
        "exists": all_ids.len(), "all_ids": if offset == 0 { Some(all_ids) } else { None }, "offset": offset}))
}
async fn message(sdk: Arc<LoggedInSdk>, mail_id: String) -> Result<Value> {
    let mail: Mail = sdk
        .mail_facade()
        .get_crypto_entity_client()
        .load(&tuple(&mail_id)?)
        .await
        .map_err(|_| "request_failed")?;
    let extensions = MailExtensions::new(sdk);
    let details = if mail.mailDetailsDraft.is_some() {
        extensions.load_mail_details_draft(&mail).await
    } else if mail.mailDetails.is_some() {
        extensions.load_mail_details_blob(&mail).await
    } else {
        return Err("unsupported_legacy_body");
    }
    .map_err(|_| "decryption_failed")?;
    if !details.body._errors.is_empty() {
        return Err("decryption_failed");
    }
    let attachments = extensions
        .load_attachments(&mail)
        .await
        .map_err(|_| "attachment_failed")?;
    if attachments.iter().any(|(file, _)| !file._errors.is_empty()) {
        return Err("attachment_failed");
    }
    let rows: Vec<Value> = attachments.iter().map(|(file, bytes)| json!({
        "name": file.name, "mime_type": file.mimeType, "cid": file.cid, "data": STANDARD.encode(bytes)
    })).collect();
    let address = |a: &tutasdk::entities::generated::tutanota::MailAddress| json!({"name": a.name, "address": a.address});
    Ok(json!({"header": header(&mail)?,
        "html": details.body.compressedText.or(details.body.text).ok_or("incomplete_response")?,
        "headers": details.headers.and_then(|h| h.compressedHeaders.or(h.headers)).unwrap_or_default(),
        "to": details.recipients.toRecipients.iter().map(address).collect::<Vec<_>>(),
        "cc": details.recipients.ccRecipients.iter().map(address).collect::<Vec<_>>(),
        "reply_to": details.replyTos.iter().map(|a| json!({"name":a.name,"address":a.address})).collect::<Vec<_>>(),
        "attachments": rows}))
}
async fn dispatch(request: Request) -> Result<Value> {
    if request.version != VERSION {
        return Err("unsupported_version");
    }
    let transport: Arc<dyn RestClient> =
        Arc::new(NativeRestClient::try_new().map_err(|_| "transport_failed")?);
    let sdk = Sdk::new(TUTA_URL.into(), transport, Arc::new(MemoryFiles::default()));
    match request.operation {
        Operation::Login {
            email,
            password,
            totp,
        } => login(&sdk, email, password, totp).await,
        Operation::Sync {
            session,
            folder,
            limit,
            offset,
        } => sync(resume(&sdk, session).await?, folder, limit, offset).await,
        Operation::Message { session, id } => message(resume(&sdk, session).await?, id).await,
        Operation::MarkRead {
            session,
            ids,
            is_read,
        } => {
            let ids = ids.iter().map(|s| tuple(s)).collect::<Result<Vec<_>>>()?;
            resume(&sdk, session)
                .await?
                .mail_facade()
                .set_unread_status_for_mails(ids, !is_read)
                .await
                .map_err(|_| "request_failed")?;
            Ok(json!({}))
        }
        Operation::Move {
            session,
            ids,
            destination,
        } => {
            let ids = ids.iter().map(|s| tuple(s)).collect::<Result<Vec<_>>>()?;
            // One mail per call so a failed move never hides a successful prefix.
            if ids.len() != 1 {
                return Err("invalid_move_batch");
            }
            let destination = tuple(&destination)?;
            MailExtensions::new(resume(&sdk, session).await?)
                .move_mails(ids, destination)
                .await
                .map_err(|_| "request_failed")?;
            Ok(json!({}))
        }
    }
}
#[tokio::main]
async fn main() {
    let mut input = Vec::new();
    let result = match io::stdin()
        .take(MAX_REQUEST_BYTES + 1)
        .read_to_end(&mut input)
    {
        Ok(_) if input.len() <= MAX_REQUEST_BYTES as usize => {
            match serde_json::from_slice::<Request>(&input) {
                Ok(request) => tokio::time::timeout(REQUEST_TIMEOUT, dispatch(request))
                    .await
                    .unwrap_or(Err("timeout")),
                Err(_) => Err("invalid_request"),
            }
        }
        _ => Err("invalid_request"),
    };
    let response = match result {
        Ok(result) => json!({"version": VERSION, "result": result}),
        Err(code) => json!({"version": VERSION, "error": {"code": code}}),
    };
    println!("{response}");
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ids_preserve_both_components() {
        assert_eq!(id(&tuple("list_1/mail-2").unwrap()), "list_1/mail-2");
        for value in ["mail", "/mail", "list/", "a/b/c", "a/../b", "a/é"] {
            assert!(tuple(value).is_err());
        }
    }
    #[test]
    fn request_rejects_unrecognized_operations() {
        assert!(serde_json::from_str::<Request>(r#"{"version":1,"op":"send"}"#).is_err());
    }
    #[tokio::test]
    async fn incompatible_version_never_connects() {
        let request = serde_json::from_str(
            r#"{"version":2,"op":"login","email":"a@b","password":"fixture"}"#,
        )
        .unwrap();
        assert_eq!(dispatch(request).await.unwrap_err(), "unsupported_version");
    }
}
