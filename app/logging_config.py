import logging
import re
import sys

# Patterns that might contain sensitive keys, tokens, or credentials
SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[a-zA-Z0-9_\-\.]{8,}"),
    re.compile(r"(?i)(x-api-key\s*[:=]\s*['\"]?)[a-zA-Z0-9_\-]{6,}(['\"]?)"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*['\"]?)[a-zA-Z0-9_\-]{6,}(['\"]?)"),
    re.compile(r"(?i)(token\s*[:=]\s*['\"]?)[a-zA-Z0-9_\-]{8,}(['\"]?)"),
    re.compile(r"(?i)(secret\s*[:=]\s*['\"]?)[a-zA-Z0-9_\-]{6,}(['\"]?)"),
    re.compile(r"(?i)(password\s*[:=]\s*['\"]?)[^'\"\s]{4,}(['\"]?)"),
]


class SecretMaskingFilter(logging.Filter):
    """Ensure no secrets, tokens or credentials ever leak into log outputs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pat in SECRET_PATTERNS:
                record.msg = pat.sub(r"\1[REDACTED]\2" if r"\2" in pat.pattern else r"\1[REDACTED]", record.msg)
        if record.args and isinstance(record.args, tuple):
            new_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    for pat in SECRET_PATTERNS:
                        arg = pat.sub(r"\1[REDACTED]\2" if r"\2" in pat.pattern else r"\1[REDACTED]", arg)
                new_args.append(arg)
            record.args = tuple(new_args)
        return True


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if getattr(root, "_egx_configured", False):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(SecretMaskingFilter())
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep request URLs/params out of INFO logs
    root._egx_configured = True  # type: ignore[attr-defined]

