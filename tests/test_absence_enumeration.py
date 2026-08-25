"""An exhaustive file enumeration covers the directory it enumerates.

Class A defect from the luna probe: RF-016 named all five files under
src/main/java as ABSENCE_OF_CHANGE and the guard still reported
IA-MISSING-ABSENCE-OF-CHANGE, because a child scope cannot prefix-match its
parent. The model's evidence was semantically complete.
"""

from sweforge.reviewer import _absence_scope_covers_target

PKG = "src/main/java/com/sweforge/pricing"
FILES = [
    f"{PKG}/DiscountPolicy.java",
    f"{PKG}/PricingCalculator.java",
    f"{PKG}/Item.java",
    f"{PKG}/Order.java",
    f"{PKG}/CustomerTier.java",
]
TARGET = "src/main/java"
TEST_FILE = "src/test/java/com/sweforge/pricing/PricingCalculatorTest.java"


def _covered(scopes, target, changed):
    return all(
        any(_absence_scope_covers_target(scope, target, changed) for scope in scopes)
        for _ in (target,)
    )


def test_enumerated_files_cover_their_directory():
    assert _covered(FILES, TARGET, {TEST_FILE}), "the RF-016 evidence was rejected"


def test_a_directory_scope_still_covers_the_directory():
    assert _absence_scope_covers_target(TARGET, TARGET, {TEST_FILE})


def test_an_ancestor_scope_still_covers_the_directory():
    assert _absence_scope_covers_target("src/main", TARGET, {TEST_FILE})


def test_a_changed_file_under_the_target_defeats_the_enumeration():
    """Positive control: the rule must not accept absence over a changed tree,
    or it would satisfy any requirement regardless of the diff."""
    changed = {f"{PKG}/DiscountPolicy.java"}
    assert not _covered(FILES, TARGET, changed)


def test_a_scope_outside_the_target_never_covers_it():
    """A quiet unrelated directory must not vacuously satisfy the target."""
    assert not _absence_scope_covers_target("src/other/Thing.java", TARGET, {TEST_FILE})


def test_enumeration_is_refused_without_a_diff_to_check_against():
    """A check that cannot observe must not pass."""
    assert not _absence_scope_covers_target(FILES[0], TARGET, None)


def test_a_file_target_is_not_treated_as_a_directory():
    """src/main/java/Foo.java has an extension, so enumeration cannot apply."""
    assert not _absence_scope_covers_target(
        f"{PKG}/Item.java", f"{PKG}/DiscountPolicy.java", {TEST_FILE}
    )
