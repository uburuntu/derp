from derp.agent.images import ImageJob, Portrait


def test_a_job_round_trips_through_its_stored_args() -> None:
    job = ImageJob(
        "a dungeon party",
        "16:9",
        references=(7,),
        avatar_of=(Portrait(1, "Anya"), Portrait(2, "Jo @jo")),
        faceless=("Bob",),
        chat_photo=True,
    )
    assert ImageJob.from_args(job.as_args()) == job


def test_a_job_without_optional_args_loads_with_defaults() -> None:
    assert ImageJob.from_args({"prompt": "a knight"}) == ImageJob("a knight")


def test_the_cast_note_numbers_people_after_the_other_references() -> None:
    job = ImageJob("x", avatar_of=(Portrait(1, "Anya"), Portrait(2)), faceless=("Bob", "Kim"))
    assert job.cast_note(first=3) == (
        "People: reference image 3 is Anya, reference image 4 is the person (profile photos). "
        "Keep each likeness on that person's character. No photo of Bob, Kim: draw them from the description."
    )
    assert ImageJob("x").cast_note(first=1) == ""
