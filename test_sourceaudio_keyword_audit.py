import csv
import json
import tempfile
import unittest
import logging
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sourceaudio_keyword_audit import audit_keyword_refresh, outstanding_keyword_revisions


class KeywordAuditTests(unittest.TestCase):
    def test_domo_export_preserves_revision_before_replacing_metadata(self):
        import domo_exports
        from domo_api import DomoQueryResult
        from domo_projection_contracts import ProjectionField

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "Metadata" / "metadata.csv"
            metadata.parent.mkdir()
            metadata.write_text("External Id,Keywords\n1,Green Music\n")
            contract = SimpleNamespace(
                fields=(ProjectionField("External Id", "id"), ProjectionField("Keywords", "tags")),
                date_column=None, distinct=False, where_sql=None, unique_by=("External Id",),
                dataset_id="synthetic",
            )
            with patch.dict(domo_exports.DOMO_PROJECTION_CONTRACTS, {"test": contract}), patch.object(
                domo_exports, "query_dataset", return_value=DomoQueryResult(["id", "tags"], [[1, "Produced with Purpose"]])
            ):
                domo_exports._export_api_projection(
                    {"key": "test", "sourceaudio_delta": "us"}, metadata,
                    SimpleNamespace(specials_dir=root), logging.getLogger("keyword-test"),
                )
            revisions = list((root / "_WORKFLOW" / "sourceaudio_keyword_revisions" / "us").glob("*.json"))
            self.assertEqual(len(revisions), 1)
            self.assertEqual(json.loads(revisions[0].read_text())["changes"][0]["old_keywords"], "Green Music")
            self.assertIn("Produced with Purpose", metadata.read_text())

    def test_keyword_change_survives_repeated_refresh_without_audio_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "metadata.csv"
            headers = ["External Id", "Filename", "Keywords", "Description"]
            def write(value):
                with metadata.open("w", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(headers)
                    writer.writerow(["123.0", "same.aif", value, "old copy"])
            write("Social Impact,Green Music")
            rows = [["123", "same.aif", "Social Impact,Produced with Purpose", "new copy"]]
            revision = audit_keyword_refresh(metadata, headers, rows, root / "audit")
            self.assertEqual(audit_keyword_refresh(metadata, headers, rows, root / "audit"), revision)
            evidence = json.loads(revision.read_text())
            self.assertEqual(evidence["status"], "requires_remote_comparison")
            self.assertEqual(evidence["changes"][0]["external_id"], "123")
            write("Social Impact,Produced with Purpose")
            self.assertIsNone(audit_keyword_refresh(metadata, headers, rows, root / "audit"))
            self.assertTrue(revision.exists())
            self.assertEqual(outstanding_keyword_revisions(root / "audit"), [revision])
            receipt = revision.with_suffix(".receipt.json")
            receipt.write_text(json.dumps({"status": "remote_verified", "revision_sha256": "wrong", "verified_external_ids": ["123"]}))
            self.assertEqual(outstanding_keyword_revisions(root / "audit"), [revision])
            receipt.write_text(json.dumps({"status": "remote_verified", "revision_sha256": hashlib.sha256(revision.read_bytes()).hexdigest(), "verified_external_ids": ["123"]}))
            self.assertEqual(outstanding_keyword_revisions(root / "audit"), [])

    def test_missing_baseline_and_conflicting_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            headers = ["External Id", "Keywords"]
            revision = audit_keyword_refresh(root / "absent.csv", headers, [["1", "tag"]], root)
            self.assertEqual(json.loads(revision.read_text())["status"], "baseline_unverified")
            self.assertEqual(outstanding_keyword_revisions(root), [revision])
            with self.assertRaises(ValueError):
                audit_keyword_refresh(root / "absent.csv", headers, [["1", "a"], ["1.0", "b"]], root)

    def test_description_only_changes_are_ignored_and_clears_require_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "metadata.csv"
            metadata.write_text("External Id,Keywords,Description\n1,tag,old\n")
            headers = ["External Id", "Keywords", "Description"]
            self.assertIsNone(audit_keyword_refresh(metadata, headers, [["1", "tag", "new"]], root))
            revision = audit_keyword_refresh(metadata, headers, [["1", "", "new"]], root)
            self.assertTrue(json.loads(revision.read_text())["changes"][0]["requires_clear_review"])


if __name__ == "__main__":
    unittest.main()
