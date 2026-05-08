"""Aliyun STS provider — Phase E5.

Calls Aliyun STS `AssumeRole` with a per-call inline policy that restricts
the temporary credentials to the user's own prefix only. The role itself
must be pre-created in Aliyun and trust the backend's main credentials.

NOT covered by automated tests in this repo — needs real Aliyun creds and
a configured role to exercise. Tested live during deployment.

SDK dependency: `aliyun-python-sdk-core` + `aliyun-python-sdk-sts`. Imported
lazily so dev environments without the SDK still load this module.
"""

from __future__ import annotations

import json
import uuid

import structlog

from app.application.ports.sts_token import StsAction, StsCredentials
from app.config.settings import Settings

logger = structlog.get_logger(__name__)


class AliyunStsProvider:
    def __init__(self, settings: Settings) -> None:
        # Strict required-config check: misconfigured prod here means STS
        # silently returns the main backend creds, which would be a major
        # security incident. Refuse to construct.
        for field in (
            "ALIYUN_STS_ACCESS_KEY_ID",
            "ALIYUN_STS_ACCESS_KEY_SECRET",
            "ALIYUN_STS_ROLE_ARN",
            "ALIYUN_OSS_REGION",
            "ALIYUN_OSS_ENDPOINT",
        ):
            if not getattr(settings, field, None):
                raise ValueError(
                    f"AliyunStsProvider requires {field} to be set"
                )
        self.settings = settings

    async def issue(
        self,
        *,
        user_id: uuid.UUID,
        bucket: str,
        action: StsAction,
        ttl_seconds: int = 900,
    ) -> StsCredentials:
        # Lazy import so dev/test environments without the SDK don't fail
        # on module load; only the prod path that constructs this class
        # has the SDK as a required runtime dep.
        from aliyunsdkcore.client import AcsClient
        from aliyunsdksts.request.v20150401 import AssumeRoleRequest

        prefix = f"users/{user_id}/{action}/"
        policy = _per_user_policy(bucket=bucket, prefix=prefix, action=action)

        client = AcsClient(
            self.settings.ALIYUN_STS_ACCESS_KEY_ID,
            self.settings.ALIYUN_STS_ACCESS_KEY_SECRET,
            self.settings.ALIYUN_OSS_REGION,
        )
        req = AssumeRoleRequest.AssumeRoleRequest()
        req.set_RoleArn(self.settings.ALIYUN_STS_ROLE_ARN)
        # SessionName is required + bounded; using user_id makes audit
        # logs link back to the requesting user even after the token is
        # revoked.
        req.set_RoleSessionName(f"aidrobe-{user_id}-{action}")
        req.set_Policy(json.dumps(policy))
        req.set_DurationSeconds(ttl_seconds)
        req.set_accept_format("JSON")

        resp_raw = client.do_action_with_exception(req)
        resp = json.loads(resp_raw)
        creds = resp["Credentials"]
        logger.info(
            "sts.issued",
            user_id=str(user_id),
            action=action,
            bucket=bucket,
            ttl_seconds=ttl_seconds,
            expiration=creds["Expiration"],
        )
        return StsCredentials(
            access_key_id=creds["AccessKeyId"],
            access_key_secret=creds["AccessKeySecret"],
            security_token=creds["SecurityToken"],
            expiration=creds["Expiration"],
            bucket=bucket,
            region=self.settings.ALIYUN_OSS_REGION,
            endpoint=self.settings.ALIYUN_OSS_ENDPOINT,
            object_key_prefix=prefix,
        )


def _per_user_policy(*, bucket: str, prefix: str, action: StsAction) -> dict[str, object]:
    """Restrict credentials to a single bucket prefix + verb. Tighter than
    the role's own policy — the inline policy ANDs with the role's, so
    these tokens are always strictly more limited than the trusting role."""
    if action == "upload":
        actions = ["oss:PutObject", "oss:AbortMultipartUpload"]
    else:
        actions = ["oss:GetObject"]
    return {
        "Version": "1",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": actions,
                # Aliyun ARN format: acs:oss:*:*:<bucket>/<key>
                "Resource": [f"acs:oss:*:*:{bucket}/{prefix}*"],
            }
        ],
    }
