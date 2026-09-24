from dataclasses import dataclass


@dataclass
class SendResult:
    ok: bool
    provider_id: str = ""
    error: str = ""
    hard_bounce: bool = False
    retryable: bool = True


class EmailProvider:
    name = "base"
    def send(self, *, to: str, subject: str, body: str, content_type: str, from_address: str, from_name: str, reply_to: str, headers: dict[str, str] | None = None) -> SendResult:
        raise NotImplementedError
