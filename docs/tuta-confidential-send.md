# Tuta confidential (end-to-end encrypted) send: spike (issue #76)

Verdict: **docs-only, nothing implemented.** The pinned SDK exposes the
*shapes* of the recipient-key path but no constructible route to it, a
newer SDK exposes nothing more, and reimplementing the crypto outside the
SDK is rejected below. The composer's Secure arm keeps refusing with
`confidential_send_unsupported`, and ordinary delivery still requires
explicit consent -- no silent downgrade anywhere.

All SDK paths below are relative to the prepared tree
`native/tuta/upstream/sdk/generated/tuta-sdk/rust/sdk/src/` at the pinned
base (TutaBridge `da12fd4`, tutanota `f5e2d86`, release `362.261020.0`,
2026-10-20). TS references are files in `native/tuta/upstream/tuta-repo`
at the same commit.

## What confidential send needs

Reference: `sendDraft` in
`src/applications/common/api/worker/facades/lazy/MailFacade.ts` (line 614)
plus `encryptBucketKeyForInternalRecipient` in
`src/platform-kit/base/base-crypto/CryptoFacade.ts` (line 638), both at
the pinned commit. A confidential mail is a draft with `confidential`
set, sent with a fresh random bucket key:

- `bucketEncMailSessionKey` and one `bucketEncFileSessionKey` per
  attachment: session keys wrapped with the bucket key.
- Per internal recipient: load the current public key through the
  *verifying* provider, asymmetrically encrypt the bucket key, and file
  one `InternalRecipientKeyData` (`pubEncBucketKey`, protocol version,
  recipient/sender key versions). External senders use a symmetric
  variant instead.
- Per secure-external (password) recipient: derive keys from the
  contact's preshared password, provision or reuse the external user, and
  file one `SecureExternalRecipientKeyData`.
- All-TutaCrypt recipient sets additionally set
  `sessionEncEncryptionAuthStatus`.
- Unknown recipients and key-verification mismatches are collected and
  thrown (`RecipientsNotFoundError`, `KeyVerificationMismatchError`) --
  the official client never downgrades a confidential send to ordinary.

## What the pinned SDK does expose

The public surface looks promising until construction time:

- `crypto::asymmetric_crypto_facade::AsymmetricCryptoFacade` (`pub mod`,
  `crypto.rs:26`): `pub fn new` (`asymmetric_crypto_facade.rs:90`),
  `asym_encrypt_sym_key` covering legacy RSA *and* TutaCrypt (`:238`),
  `tuta_crypt_encrypt_sym_key` (`:298`), `authenticate_sender` (`:111`).
- `crypto::public_key_provider::PublicKeyProvider` (`pub mod`,
  `crypto.rs:27`): `pub fn new`, `load_current_pub_key`,
  `load_versioned_pub_key` over `PublicKeyService`.
- Raw `PublicKeyService` GET through
  `LoggedInSdk::get_service_executor` (`lib.rs:442`) -- `main.rs`
  already uses this for internal/external classification.
- The wire side is fully public: `SendDraftData` with
  `bucketEncMailSessionKey`, `internalRecipientKeyData`,
  `symEncInternalRecipientKeyData`, `secureExternalRecipientKeyData`,
  `sessionEncEncryptionAuthStatus` (`entities/generated/tutanota.rs`).

## What blocks, exactly

1. **Neither facade is constructible.** Both constructors take
   `Arc<ServiceExecutor>`. The only executor a caller can reach is
   `&Arc<ResolvingServiceExecutor>` from `get_service_executor`, and
   `ResolvingServiceExecutor` is a tuple struct with a private field
   (`services/service_executor.rs:27`) -- there is no accessor and
   `Deref` cannot mint an `Arc` of the inner type. Building a fresh
   `ServiceExecutor::new` needs the logged-in auth headers provider and
   REST client, neither of which `LoggedInSdk` exposes. This alone
   closes the path.
2. **The key loader is unnameable.** `AsymmetricCryptoFacade::new` also
   takes `Arc<KeyLoaderFacade>`, but `mod key_loader_facade` is private
   (`lib.rs:70`). `CryptoFacade::get_key_loader_facade` is `pub`
   (`crypto/crypto_facade.rs:81`) and returns the right value, so this
   argument could in principle be smuggled through type inference --
   but the `ServiceExecutor` argument cannot, so the inference trick
   leads nowhere.
3. **The key primitives are crate-private.** `mod x25519` and
   `mod tuta_crypt` are fully private, `mod rsa` and `mod kyber` are
   `pub(crate)` (`crypto.rs:20-24`). The `PublicKey` enum itself is
   public (`crypto::key`), but every variant wraps one of those
   private types, so a caller can neither parse a `PublicKeyGetOut`
   into keys nor encrypt with them. Even a legacy-RSA-only send --
   the "some recipients" the refusal comment worried about -- is
   unimplementable, not merely undesirable.
4. **No public sender-identity entry.**
   `get_or_make_sender_identity_key_pair` is a private method
   (`asymmetric_crypto_facade.rs:354`); the TutaCrypt arm of
   `asym_encrypt_sym_key` reaches it only through the key loader.
5. **Key verification has no Rust implementation at all.** The TS send
   path loads keys through `PublicEncryptionKeyProvider`
   (`src/platform-kit/base/base-crypto/PublicEncryptionKeyProvider.ts:32`),
   which verifies gossip signatures and TOFU-persists bindings via
   `KeyVerificationFacade`, throwing `KeyVerificationMismatchError`.
   Neither type exists in the Rust SDK. The Rust `PublicKeyProvider`
   ignores the `signature` field (`signature: _`), and
   `authenticate_sender` merely re-checks the key against
   `PublicKeyService` -- TOFU without persistence, by its own
   docstring ("When we have key verification we should stop verifying
   against the PublicKeyService...").
6. **No Rust send API.** The Rust `MailFacade` (`mail_facade.rs`)
   offers mailbox/folder/move/read calls only; `sendDraft` and
   `addRecipientKeyData` are TS-only (`MailFacade.ts:614,818`), as are
   external-user provisioning (`getExternalGroupKeys`,
   `createExternalUser`) and contact passwords.

## Options investigated

- **Compose the facades from public pieces.** Fails on (1): no
  `Arc<ServiceExecutor>` is obtainable, authenticated or otherwise.
- **Raw `PublicKeyService` plus our own crypto.** Rejected. It means
  reimplementing TutaCrypt encapsulation (X25519 + Kyber composition,
  KDF, wire format) and RSA handling against third-party crates, plus
  inventing the verification story (5) with no Rust reference. An
  unaudited PQ-crypto reimplementation is not a spike-sized change and
  must not ship ahead of upstream review.
- **Re-pin to a newer SDK (option b).** Dead. Latest tutanota release
  at spike time is `365.261103.0` (2026-11-04), two weeks past the pin;
  upstream `master` was checked file by file
  (`tuta-sdk/rust/sdk/src/crypto.rs`, `lib.rs`, `mail_facade.rs`): the
  visibility is identical (`mod key_loader_facade` still private, no
  Rust send API), and `crypto/public_encryption_key_provider.rs`
  does not exist upstream either. TutaBridge `HEAD` (`4101a8c`) is two
  bridge-app commits past our pin and still on SDK base
  `362.261020.0`. A re-pin buys nothing for this issue.
- **A 13th SDK patch exposing the facades.** Technically the smallest
  unblock (e.g. a `LoggedInSdk`/`MailFacade` accessor returning a
  constructed `AsymmetricCryptoFacade`, or `pub mod key_loader_facade`
  plus a `ServiceExecutor` accessor), but `sdk/patches/` lives in the
  upstream TutaBridge submodule -- it cannot land from this repo, and
  hand-editing `sdk/generated/` is overwritten by `sdk-prepare.py`.
  It would also unblock encryption only, leaving verification (5)
  unaddressed.

## What would unblock this

1. Upstream (tutao or a TutaBridge SDK patch) exposes a constructed
   `AsymmetricCryptoFacade` -- or, equivalently, makes
   `key_loader_facade` public and adds a `ServiceExecutor` accessor --
   so recipient-key lookup and bucket encryption go through the audited
   SDK path including the sender-identity flow.
2. A Rust key-verification story: port `PublicEncryptionKeyProvider`
   and `KeyVerificationFacade` (or define TOFU persistence plus gossip
   `signature` checks) so `KeyVerificationMismatchError` exists before
   the first confidential byte is sent.
3. Scope the first implementation to internal recipients: secure
   externals need contact passwords and external-user provisioning,
   both TS-only today. Any recipient that cannot be served -- unknown
   address, verification mismatch, external without a password -- must
   refuse loudly, never downgrade: the `confidential_send_unsupported`
   refusal and the `ordinary_confirmed` consent gate stay until all
   three are in place.

## Current behavior (unchanged)

`send` in `native/tuta/src/main.rs` returns
`confidential_send_unsupported` for `Delivery::Secure` before touching
the network, and `ordinary_delivery_requires_consent` for ordinary
sends without the composer's explicit confirmation. The Secure radio
stays wired to that refusal; capability stays per-account.
