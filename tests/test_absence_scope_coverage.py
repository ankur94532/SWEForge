"""A verified-unchanged directory subsumes the files beneath it.

Requirement prose enumerates bare filenames ("DiscountPolicy.java") while the
evidence schema demands repository-relative paths. Requiring both a directory
scope and each of its own children is redundant: the absence binding already
checked the directory against authoritative changed_files.
"""

from sweforge.reviewer import (
    EvidenceKind,
    EvidenceRef,
    _absence_scope_covers_target,
    _has_required_absence_refs,
)


def _absence(path: str) -> EvidenceRef:
    return EvidenceRef(
        ref_id=f"r-{path}",
        requirement_id="plan:step:6",
        kind=EvidenceKind.ABSENCE_OF_CHANGE,
        path=path,
        source_id="",
    )


UNCHANGED = {"src/test/java/com/sweforge/pricing/PricingCalculatorTest.java"}


def test_directory_scope_covers_a_bare_filename_that_did_not_change():
    assert _absence_scope_covers_target(
        "src/main/java", "DiscountPolicy.java", UNCHANGED
    )


def test_directory_scope_does_not_vacuously_cover_a_changed_file():
    """An unrelated quiet directory must not satisfy a file that did change."""
    changed = {"src/main/java/com/sweforge/pricing/DiscountPolicy.java"}
    assert not _absence_scope_covers_target("docs", "DiscountPolicy.java", changed)


def test_directory_subsumption_requires_changed_files_to_be_known():
    assert not _absence_scope_covers_target("src/main/java", "DiscountPolicy.java")


def test_exact_basename_still_covers():
    assert _absence_scope_covers_target(
        "src/main/java/com/sweforge/pricing/DiscountPolicy.java", "DiscountPolicy.java"
    )


def test_a_different_file_does_not_cover():
    assert not _absence_scope_covers_target(
        "src/main/java/com/sweforge/pricing/Order.java", "DiscountPolicy.java"
    )


def test_directory_target_still_needs_a_covering_scope():
    assert _absence_scope_covers_target("src/main", "src/main/java")
    assert not _absence_scope_covers_target("src/test", "src/main/java")


DIRECTORY_AND_PARENTHETICAL = (
    "Do not modify any file under /src/main/java (DiscountPolicy.java, "
    "PricingCalculator.java, Item.java) - production code stays unchanged."
)
NAMED_FILES_ONLY = (
    "Do not modify DiscountPolicy.java, PricingCalculator.java, or any other "
    "production source file."
)


def test_directory_ref_alone_satisfies_a_parenthetical_enumeration():
    """RF-016 shape: the parenthetical glosses the directory, it is not extra scope."""
    assert _has_required_absence_refs(
        [_absence("src/main/java")], DIRECTORY_AND_PARENTHETICAL, UNCHANGED
    )


def test_directory_ref_alone_satisfies_named_files():
    """RF-7 shape: no directory target in the prose, but src/main subsumes both."""
    assert _has_required_absence_refs(
        [_absence("src/main")], NAMED_FILES_ONLY, UNCHANGED
    )


def test_per_file_refs_still_satisfy():
    refs = [
        _absence("src/main/java/com/sweforge/pricing/DiscountPolicy.java"),
        _absence("src/main/java/com/sweforge/pricing/PricingCalculator.java"),
        _absence("src/main/java"),
    ]
    assert _has_required_absence_refs(refs, DIRECTORY_AND_PARENTHETICAL, UNCHANGED)


def test_absence_evidence_is_still_required():
    assert not _has_required_absence_refs([], DIRECTORY_AND_PARENTHETICAL)


def test_an_unrelated_file_scope_does_not_satisfy():
    refs = [_absence("src/test/java/com/sweforge/pricing/PricingCalculatorTest.java")]
    assert not _has_required_absence_refs(refs, DIRECTORY_AND_PARENTHETICAL, UNCHANGED)
