import pytest

from evalstudio.bundle import BundleError, export_bundle, import_bundle
from evalstudio.models import EvalSet, RubricSet, Target, UseCaseCard
from evalstudio.store import NotFound


def test_versions_are_append_only(store):
    p = store.create_project("demo", "Demo")
    v1 = store.save_version(p.id, "evalset", {"cases": [
        {"id": "a", "input": "x", "category": "c", "expected_behaviours": ["y"]}]})
    v2 = store.save_version(p.id, "evalset", {"cases": []}, note="emptied")
    assert (v1.version, v2.version) == (1, 2)
    assert len(store.load(p.id, "evalset", 1).cases) == 1  # old version unchanged
    assert store.get_version(p.id, "evalset").version == 2
    assert store.latest_approved(p.id, "evalset") is None
    store.approve(p.id, "evalset", 1)
    assert store.latest_approved(p.id, "evalset").version == 1


def test_invalid_data_is_not_saved(store):
    p = store.create_project("demo", "Demo")
    with pytest.raises(ValueError):
        store.save_version(p.id, "evalset", {"cases": [{"id": "BAD ID"}]})
    with pytest.raises(NotFound):
        store.get_version(p.id, "evalset")


def test_duplicate_project_slug(store):
    store.create_project("demo", "Demo")
    with pytest.raises(ValueError, match="already exists"):
        store.create_project("demo", "Again")


def test_demo_bundle_imports(store, demo_dir):
    p = import_bundle(store, demo_dir, approve=True)
    assert p.slug == "support-email"
    card = store.load(p.id, "usecase")
    evalset = store.load(p.id, "evalset")
    rubrics = store.load(p.id, "rubrics")
    target = store.load(p.id, "target")
    assert isinstance(card, UseCaseCard) and isinstance(evalset, EvalSet)
    assert isinstance(rubrics, RubricSet) and isinstance(target, Target)
    assert "Brightside" in target.config.system_prompt  # loaded from system_prompt_v1.md
    # Every rubric points at a real test case, and every case category is on the card.
    ids = {c.id for c in evalset.cases}
    assert all(r.test_case_id in ids for r in rubrics.rubrics)
    assert {c.category for c in evalset.cases} <= set(card.categories)


def test_reimport_only_versions_what_changed(store, demo_dir, tmp_path):
    import shutil

    import_bundle(store, demo_dir)
    p = import_bundle(store, demo_dir, approve=True)  # unchanged: no new versions, just approved
    assert store.get_version(p.id, "evalset").version == 1
    assert store.get_version(p.id, "evalset").status == "approved"
    copy = tmp_path / "bundle"
    shutil.copytree(demo_dir, copy)
    text = (copy / "system_prompt_v1.md").read_text(encoding="utf-8")
    (copy / "system_prompt_v1.md").write_text(text + "\nBe brief.\n", encoding="utf-8")
    import_bundle(store, copy)
    assert store.get_version(p.id, "target").version == 2 and store.get_version(p.id, "evalset").version == 1


def test_export_then_import_round_trip(store, demo_dir, tmp_path):
    kinds = ("usecase", "evalset", "rubrics", "target")
    p = import_bundle(store, demo_dir)
    original = {k: store.load(p.id, k) for k in kinds}
    for fmt in ("yaml", "json"):
        out = tmp_path / fmt
        export_bundle(store, p.slug, out, fmt=fmt)
        store.delete_project(p.slug)
        p = import_bundle(store, out)
        assert {k: store.load(p.id, k) for k in kinds} == original, fmt


def test_bad_bundle_explains_the_problem(store, tmp_path):
    (tmp_path / "project.yaml").write_text("slug: broken\n", encoding="utf-8")
    (tmp_path / "evalset.yaml").write_text("cases:\n  - id: a\n    input: hi\n", encoding="utf-8")
    with pytest.raises(BundleError, match=r"(?s)evalset.yaml has problems:.*cases > 0 > category: Field required"):
        import_bundle(store, tmp_path)
