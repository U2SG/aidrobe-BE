"""V3-013 — avatar request/response shapes, frozen by contract §4.2.

`AvatarProfileResponse` is a transcription of the frozen `GET /v1/avatar-profile`
JSON, field for field. Nothing is added to it: FE reads it against the contract,
and an extra field is a contract change even when it is harmless. Notably the
current consent **copy version** is not returned here — the frozen shape's
`consent` block reports the version the user already accepted, not the one to
show next. FE owns the copy it renders (V3-014), and adding a "current version"
field would let the two disagree about who decides what the user is shown.

**Acceptance 4 is a property of this file** ("No API claims sizing or biometric
accuracy"). There is no size, fit, measurement, body-shape, age or weight field
anywhere in these models, and no free-text field a service could write one into:
every string here is an id, a status from a frozen enum, a timestamp or a signed
URL. `test_avatar_api_makes_no_body_claims` walks the generated OpenAPI for the
avatar paths and fails on any such property name, so the guarantee is checked
against what a client actually sees rather than against this docstring.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AvatarActiveVersionModel(BaseModel):
    """`active_version` — null unless status is ready/refresh_recommended."""

    id: str
    created_at: datetime
    #: Short-lived signed URL, signed at read time. Never persisted by the client
    #: (V3_SPEC §13.5) and never stored in this shape by us.
    canonical_front_url: str | None = None


class AvatarCandidateModel(BaseModel):
    """`candidate` — material awaiting a consent decision; null while preparing.

    Not restricted to `status=candidate`: a revoked or already-ready profile can
    carry one too, which is what makes §4.1's `disabled → preparing` re-enable
    and §4.2's `replace` from a new photo reachable. `_render_profile` carries
    the full rationale.
    """

    source_asset_id: str
    source_type: str
    preview_url: str | None = None


class AvatarConsentModel(BaseModel):
    """`consent` — null until first consent.

    Together these two fields are V3-013 acceptance 1 ("Consent version and
    timestamp are recorded") as seen from outside: the version is the copy the
    user actually accepted, and the timestamp is when.
    """

    version: str
    consented_at: datetime


class AvatarProfileResponse(BaseModel):
    status: str
    active_version: AvatarActiveVersionModel | None = None
    candidate: AvatarCandidateModel | None = None
    consent: AvatarConsentModel | None = None


class AvatarConsentRequest(BaseModel):
    """`POST …/consent` body: `{"accept": true|false}`.

    `consent_version` is required when accepting and forbidden otherwise — the
    frozen §4.2 rule ("`accept=true` requires the consent copy version"). It is
    validated in the router rather than by a model validator so the refusal
    carries the same `4001 VALIDATION_ERROR` envelope as every other business
    rule, instead of FastAPI's raw 422 shape.

    Bounded at 64 characters to match `avatar_source_assets.consent_version`, so
    an over-long value is refused at the edge rather than truncated by the
    database.
    """

    accept: bool
    consent_version: str | None = Field(default=None, min_length=1, max_length=64)


class AvatarConsentResponse(BaseModel):
    status: str
    source_asset_id: str
    consent_status: str
    #: Echoed back so a client can confirm which copy version was recorded —
    #: the audit value, not the one it should show next.
    consent_version: str | None = None
    consented_at: datetime | None = None


class AvatarUploadRegistrationRequest(BaseModel):
    """`POST /v1/avatar-profile/uploads` — source C (V3_SPEC §7.1).

    Carries the reference to an object the client has *already* uploaded through
    the existing presign flow, which is what contract §4.2 specifies ("Reuses the
    existing presign upload flow; body carries the uploaded asset reference").
    Either the `object_key` from `POST /v1/uploads/presign` or the `file_url` it
    returned alongside — clients hold both, and accepting only one of them would
    make this endpoint depend on which field a client happened to keep.

    Ownership of whatever this resolves to is enforced in the router: this is a
    free-form client-supplied reference, the exact shape of the cross-user read
    V3-012's round-3 review found.
    """

    object_key: str | None = Field(default=None, min_length=1, max_length=512)
    file_url: str | None = Field(default=None, min_length=1, max_length=2048)


class AvatarUploadRegistrationResponse(BaseModel):
    #: Queued/duplicate/etc. — the closed vocabulary from V3-012's
    #: `EnqueueOutcome`, so the client can tell "we are looking at it" from
    #: "nothing will happen" instead of polling blindly.
    detection_outcome: str
    status: str


class AvatarPrepareResponse(BaseModel):
    status: str
    job_id: str
    source_asset_id: str
    #: False when an identical preparation was already queued. Acceptance 2 seen
    #: from outside: a repeated request is visibly the *same* job, not a second
    #: one, and never a second charge.
    created: bool


class AvatarDeleteResponse(BaseModel):
    status: str
    #: Echoed because contract §4.2 requires the caller to decide it explicitly
    #: and V3_SPEC §13.5 requires being unambiguous about whether derived assets
    #: go too. See `delete_avatar` for what it governs today.
    purge_derived: bool
    #: How many storage objects the scheduled purge will remove. A count, never
    #: the keys (contract §7).
    scheduled_object_count: int
    purge_job_id: str | None = None
