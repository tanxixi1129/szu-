import base64
import io
import sqlite3
import unittest
import zipfile
from pathlib import Path

import app


class RagAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.original_connect = app.connect_db
        self.original_data_dir = app.DATA_DIR
        self.original_remote_enabled = app.REMOTE_ENABLED
        app.connect_db = lambda: self.connection
        app.DATA_DIR = Path(".")
        app.REMOTE_ENABLED = False
        app.init_db()

    def tearDown(self) -> None:
        app.connect_db = self.original_connect
        app.DATA_DIR = self.original_data_dir
        app.REMOTE_ENABLED = self.original_remote_enabled
        self.connection.close()

    def test_ingest_search_duplicate_and_delete(self) -> None:
        raw = "星河项目将在十月十五日交付。负责人是林晓。验收需要通过安全测试。".encode()
        created = app.ingest_document("计划.md", raw)

        self.assertEqual(created["chunks"], 1)
        self.assertFalse(created["duplicate"])
        self.assertEqual(len(app.list_documents()), 1)

        results = app.search_knowledge("项目什么时候交付？", top_k=3)
        self.assertEqual(results[0]["name"], "计划.md")
        self.assertIn("十月十五日", results[0]["content"])

        duplicate = app.ingest_document("计划副本.md", raw)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(app.list_documents()), 1)

        self.assertTrue(app.delete_document(created["id"]))
        self.assertEqual(app.list_documents(), [])

    def test_docx_text_extraction_without_third_party_package(self) -> None:
        document_xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>第一段</w:t></w:r></w:p>
          <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body>
        </w:document>'''.encode("utf-8")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document_xml)

        text = app.extract_text("sample.docx", buffer.getvalue())
        self.assertEqual(text, "第一段\n\nSecond paragraph")

    def test_response_text_parser(self) -> None:
        response = {"output": [{"content": [{"type": "output_text", "text": "有依据的回答"}]}]}
        self.assertEqual(app.parse_response_text(response), "有依据的回答")

    def test_rejects_invalid_upload(self) -> None:
        with self.assertRaisesRegex(ValueError, "暂不支持"):
            app.ingest_document("archive.exe", base64.b64decode("AA=="))


if __name__ == "__main__":
    unittest.main()
