import io
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen

import estrattore_pdf_webapp as app


class ExtractionHelpersTests(unittest.TestCase):
    def test_parse_upload_decodes_fields_and_sanitizes_filename(self):
        boundary = "test-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="document"; filename="../../fattura.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
            "pdf-bytes\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            "llama3.2\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        result = app.parse_upload(body, f"multipart/form-data; boundary={boundary}")

        self.assertEqual(result, (b"pdf-bytes", "fattura.pdf", "llama3.2", ""))

    def test_ocr_result_accepts_rapidocr_result_tuple(self):
        class Engine:
            def __call__(self, image):
                return ([([[0, 0]], "Fattura 42", 0.99), ([[0, 1]], "Totale 10", 0.98)], 0.1)

        class Image:
            shape = (10, 10, 3)

        self.assertEqual(app._ocr_image(Image(), Engine()), "Fattura 42\nTotale 10")

    @patch("estrattore_pdf_webapp.urllib.request.urlopen")
    def test_ollama_extract_returns_structured_json(self, urlopen_mock):
        response = io.BytesIO(
            json.dumps({"message": {"content": '{"numero": "42"}'}}).encode()
        )
        response.__enter__ = lambda self=response: self
        response.__exit__ = lambda *args: False
        urlopen_mock.return_value = response

        result = app.ollama_extract("Fattura numero 42", "llama3.2", "Estrai il numero")

        self.assertEqual(result, {"numero": "42"})
        request = urlopen_mock.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["format"], "json")
        self.assertIn("Fattura numero 42", payload["messages"][1]["content"])

    def test_ollama_rejects_invalid_model_name(self):
        with self.assertRaises(app.ExtractionError):
            app.ollama_extract("testo", "../modello")


class WebHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_home_page_is_local_and_includes_privacy_and_upload_ui(self):
        with urlopen(self.base_url) as response:
            page = response.read().decode()

        self.assertEqual(response.status, 200)
        self.assertIn("RapidOCR", page)
        self.assertIn('id="document"', page)
        self.assertIn("Content-Security-Policy", str(response.headers))
        self.assertIn("non salvati dall’app", page)

    def test_extract_rejects_missing_document(self):
        boundary = "empty-form"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            "llama3.2\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        request = Request(
            f"{self.base_url}/extract",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )

        with self.assertRaises(Exception) as context:
            urlopen(request)

        self.assertEqual(context.exception.code, 400)
        self.assertIn("documento", context.exception.read().decode())


if __name__ == "__main__":
    unittest.main()
