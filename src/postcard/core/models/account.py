from gi.repository import GObject

# Connection security values, in the order the account dialog's ComboRows list
# them ("TLS", "STARTTLS") -- get_selected() indexes straight into this.
SECURITY_TLS = "tls"
SECURITY_STARTTLS = "starttls"
SECURITY_OPTIONS = (SECURITY_TLS, SECURITY_STARTTLS)

# TCP ports are 16-bit and 0 is not dialable.
MIN_PORT = 1
MAX_PORT = 65535

# How an account's mail is reached. IMAP/SMTP is everything typed in by hand
# and Google; Microsoft 365 hands out a token that only opens the Graph API;
# Tuta and Proton speak their own HTTPS APIs through a native helper.
PROTOCOL_IMAP = "imap"
PROTOCOL_GRAPH = "graph"
PROTOCOL_TUTA = "tuta"
PROTOCOL_PROTON = "proton"

# What a Graph account stores in the host/port columns, which predate it and are
# NOT NULL. Error messages name the host, so it is the real one.
GRAPH_HOST = "graph.microsoft.com"
GRAPH_PORT = 443

# Same for Tuta: the API endpoint the helper talks to, for error messages.
TUTA_HOST = "app.tuta.com"
TUTA_PORT = 443


def parse_port(text: str) -> int | None:
    """A port number from user input, or None when it isn't one.

    Returns None rather than raising: the caller is validating a text entry, so
    "not a port yet" is an expected state, not an error.
    """
    try:
        port = int(text.strip())
    except ValueError:
        return None
    return port if MIN_PORT <= port <= MAX_PORT else None


class Account(GObject.Object):
    __gtype_name__ = "PostcardAccount"

    def __init__(
        self,
        *,
        id: int,
        email: str,
        display_name: str,
        imap_host: str,
        imap_port: int,
        smtp_host: str,
        smtp_port: int,
        imap_security: str = "tls",
        smtp_security: str = "tls",
        username: str = "",
        goa_id: str = "",
        protocol: str = PROTOCOL_IMAP,
    ) -> None:
        super().__init__()
        self.id: int = id
        self.email: str = email
        self.display_name: str = display_name
        self.imap_host: str = imap_host
        self.imap_port: int = imap_port
        self.smtp_host: str = smtp_host
        self.smtp_port: int = smtp_port
        self.imap_security: str = imap_security
        self.smtp_security: str = smtp_security
        # The name the server wants at sign-in, when that is not the address the
        # mail is addressed to. Empty means the two are the same.
        self.username: str = username
        # Set when the account came from GNOME Online Accounts, which is then
        # where its credentials live instead of the keyring.
        self.goa_id: str = goa_id
        self.protocol: str = protocol

    @property
    def is_tuta(self) -> bool:
        return self.protocol == PROTOCOL_TUTA

    @property
    def is_proton(self) -> bool:
        return self.protocol == PROTOCOL_PROTON

    @property
    def is_graph(self) -> bool:
        return self.protocol == PROTOCOL_GRAPH

    @property
    def is_pushless(self) -> bool:
        """Whether this account's inbox can only be polled, never watched.

        IMAP IDLE holds a connection open on the inbox; Graph, Tuta and Proton
        offer no push channel a desktop client can hold, so the watcher polls
        instead. One predicate so the next protocol cannot be forgotten there.
        """
        return self.protocol in (PROTOCOL_GRAPH, PROTOCOL_TUTA, PROTOCOL_PROTON)

    @property
    def short_label(self) -> str:
        # Free to be a nickname: outgoing mail puts email in From:, never this.
        return self.display_name.strip() or self.email.partition("@")[0]

    @property
    def login_name(self) -> str:
        """What to sign in with: the address, unless the server wants its own.

        Accounts saved before there was a username field have it empty, so the
        fallback is also what keeps those signing in unchanged.
        """
        return self.username or self.email
