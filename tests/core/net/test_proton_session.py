from postcard.core.net.proton_session import (
    ERROR_CODES,
    MECHANISM_PROTON,
    ProtonError,
)


def test_the_stub_only_names_its_own_codes():
    assert "unsupported_operation" in ERROR_CODES
    assert MECHANISM_PROTON == "proton-session"


def test_the_message_names_no_server_detail():
    error = ProtonError("unsupported_operation")
    assert error.code == "unsupported_operation"
    assert "unsupported_operation" not in str(error)
