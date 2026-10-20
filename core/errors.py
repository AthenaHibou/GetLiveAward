from __future__ import annotations


class BiliRewardError(RuntimeError):
    """Raised when the reward flow fails."""


class BiliCookieInvalidError(BiliRewardError):
    """Raised when the current cookie is no longer valid."""


class BiliRequestError(BiliRewardError):
    """A transient transport failure that may be retried."""


class BiliRateLimitError(BiliRewardError):
    """Stop the batch on rate limiting or risk control."""


class BiliRewardTimeoutError(BiliRewardError):
    """The configured claim duration has elapsed."""


class LoginCancelledError(BiliRewardError):
    """Raised when the user cancels the QR login flow."""


class BiliRewardCancelledError(BiliRewardError):
    """Raised when the user cancels the award claim stage."""
