"""10.19: repository.private reaches the job payload and the run record; unknown means public."""

import copy
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, ReviewRun
from app.github.webhooks import PullRequestEvent
from app.jobs.payload import ReviewJobPayload
from tests.test_jobs import RAW
from tests.test_webhook_signatures import post_signed


def body(private: object = "keep") -> bytes:
    raw = copy.deepcopy(RAW)
    if private == "missing":
        raw["repository"].pop("private", None)
    elif private != "keep":
        raw["repository"]["private"] = private
    return json.dumps(raw).encode()


@pytest.mark.parametrize(
    ("value", "expected_private", "expected_public"),
    [(True, True, False), (False, False, True), ("missing", None, True)],
)
def test_visibility_flows_from_the_webhook_to_payload_and_run(
    db_session: Session, value: object, expected_private: bool | None, expected_public: bool
) -> None:
    assert post_signed(body(value)).json() == {"status": "accepted"}

    run = db_session.scalars(select(ReviewRun)).one()
    assert run.repo_private is expected_private
    payload = ReviewJobPayload.model_validate(db_session.scalars(select(Job)).one().payload)
    assert payload.repo_private is expected_private
    assert payload.is_public is expected_public  # unknown is public: the safe side


def test_event_property_matches_the_payload() -> None:
    event = PullRequestEvent.model_validate(json.loads(body(False)))
    assert event.repo_private is False
    assert PullRequestEvent.model_validate(json.loads(body("missing"))).repo_private is None
