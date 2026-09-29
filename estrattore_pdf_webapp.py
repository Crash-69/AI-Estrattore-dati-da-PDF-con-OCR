#!/usr/bin/env python3
"""Local web app for extracting structured data from PDFs and images."""

from __future__ import annotations

import json
import html
import os
import re
import threading
import urllib.error
import urllib.request
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOST = "127.0.0.1"
PORT = 8000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 30
MAX_IMAGE_PIXELS = 30_000_000
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
_ocr_engine = None
_ocr_lock = threading.Lock()

PAGE = """<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#101a2b">
  <title>Estrattore Dati PDF AI</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif;
      background: #101a2b; color: #edf3f8; font-synthesis: none; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: radial-gradient(ellipse at 15% 0%, #19354a 0, #101a2b 46rem); }
    main { width: min(920px, calc(100% - 32px)); margin: 0 auto; padding: 58px 0 72px; }
    header { margin-bottom: 32px; }
    .eyebrow { color: #69dbc0; font-size: .78rem; font-weight: 700; letter-spacing: .13em; text-transform: uppercase; }
    h1 { margin: 10px 0; font-size: clamp(2rem, 5vw, 3rem); letter-spacing: -.045em; }
    header p, .hint { color: #a8b7c7; line-height: 1.65; }
    .card { padding: clamp(20px, 4vw, 34px); border: 1px solid #2a3d50; border-radius: 18px;
      background: #172538eF; box-shadow: 0 24px 70px #07101b55; }
    label { display: block; margin: 18px 0 8px; font-size: .91rem; font-weight: 650; }
    input, textarea { width: 100%; padding: 13px 14px; border: 1px solid #354b60;
      border-radius: 10px; background: #101c2b; color: #edf3f8; font: inherit; }
    input[type=file] { border-style: dashed; padding: 18px; }
    textarea { min-height: 96px; resize: vertical; }
    input:focus, textarea:focus, button:focus-visible { outline: 2px solid #69dbc0; outline-offset: 2px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
    button { border: 0; border-radius: 10px; padding: 13px 19px; background: #69dbc0; color: #10202a;
      font: inherit; font-weight: 750; cursor: pointer; }
    button:disabled { cursor: wait; opacity: .65; }
    .submit { margin-top: 22px; }
    .hint { margin: 8px 0 0; font-size: .84rem; }
    .privacy { margin-top: 18px; color: #91a5b7; font-size: .82rem; }
    #status { min-height: 1.5em; margin: 18px 0 0; color: #f4c77b; }
    #result { display: none; margin-top: 24px; }
    #result h2 { margin-top: 0; }
    pre { overflow: auto; max-height: 420px; padding: 18px; border-radius: 10px; background: #0d1724;
      color: #c8f3e8; white-space: pre-wrap; overflow-wrap: anywhere; }
    .result-actions { display: flex; gap: 10px; align-items: center; margin-top: 14px; }
    .secondary { background: #25394c; color: #edf3f8; }
    @media (max-width: 620px) { main { padding-top: 36px; } .grid { grid-template-columns: 1fr; gap: 0; } }
  </style>
</head>
<body>
<main>
  <header>
    <div class="eyebrow">Elaborazione locale · dati sotto il tuo controllo</div>
    <h1>Estrattore Dati PDF AI</h1>
    <p>Trasforma documenti, scansioni e immagini in dati strutturati. L’OCR usa RapidOCR e Ollama organizza il testo in JSON.</p>
  </header>
  <section class="card" aria-labelledby="form-title">
    <h2 id="form-title">Estrai i dati dal documento</h2>
    <form id="extract-form">
      <label for="document">Documento PDF o immagine</label>
      <input id="document" name="document" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp" required>
      <p class="hint">PDF e immagini · massimo 20 MB · massimo 30 pagine per PDF</p>
      <div class="grid">
        <div><label for="model">Modello Ollama</label><input id="model" name="model" value="llama3.2" maxlength="100" required></div>
        <div><label for="ollama-url">Indirizzo Ollama</label><input id="ollama-url" value="__OLLAMA_URL__" readonly></div>
      </div>
      <label for="instructions">Cosa vuoi estrarre?</label>
      <textarea id="instructions" name="instructions" maxlength="2000" placeholder="Es. Estrai numero fattura, data, fornitore e totale."></textarea>
      <button class="submit" id="submit" type="submit">Estrai dati</button>
      <p class="privacy">I file vengono elaborati in memoria, non salvati dall’app e inviati solo all’endpoint Ollama configurato.</p>
    </form>
    <p id="status" role="status" aria-live="polite"></p>
  </section>
  <section class="card" id="result" aria-labelledby="result-title">
    <h2 id="result-title">Risultato JSON</h2>
    <pre id="json-result"></pre>
    <details><summary>Testo riconosciuto</summary><pre id="ocr-result"></pre></details>
    <div class="result-actions"><button class="secondary" id="download" type="button">Scarica JSON</button></div>
  </section>
</main>
<script>
  const form = document.querySelector("#extract-form");
  const button = document.querySelector("#submit");
  const status = document.querySelector("#status");
  const result = document.querySelector("#result");
  let latestJSON = "";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    status.textContent = "Analisi del documento in corso…";
    result.style.display = "none";
    button.disabled = true;
    try {
      const response = await fetch("/extract", { method: "POST", body: new FormData(form) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Estrazione non riuscita.");
      latestJSON = JSON.stringify(data.structured_data, null, 2);
      document.querySelector("#json-result").textContent = latestJSON;
      document.querySelector("#ocr-result").textContent = data.extracted_text;
      result.style.display = "block";
      status.textContent = "Estrazione completata.";
    } catch (error) {
      status.textContent = error.message || "Errore durante l’estrazione.";
    } finally {
      button.disabled = false;
    }
  });
  document.querySelector("#download").addEventListener("click", () => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([latestJSON], { type: "application/json" }));
    link.download = "dati-estratti.json";
    link.click();
    URL.revokeObjectURL(link.href);
  });
</script>
</body>
</html>
"""


class ExtractionError(ValueError):
    """An invalid or unsupported document or request."""


def _get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        with _ocr_lock:
            if _ocr_engine is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except ImportError as exc:
                    raise RuntimeError(
                        "RapidOCR non è installato. Esegui: pip install -r requirements.txt"
                    ) from exc
                _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_image(image, engine):
    height, width = image.shape[:2]
    if width * height > MAX_IMAGE_PIXELS:
        raise ExtractionError("L’immagine supera il limite di 30 megapixel.")
    result = engine(image)
    if isinstance(result, tuple):
        result = result[0]
    return "\n".join(str(line[1]) for line in (result or []) if len(line) > 1).strip()


def extract_document(contents: bytes, filename: str, ocr_engine=None) -> str:
    """Extract native PDF text and OCR pages/images that need recognition."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ExtractionError("Formato non supportato. Carica un PDF o un’immagine.")
    if not contents:
        raise ExtractionError("Il file è vuoto.")

    try:
        import cv2
        import fitz
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Le dipendenze OCR/PDF non sono installate. Esegui: pip install -r requirements.txt"
        ) from exc

    engine = ocr_engine

    def recognize(image):
        nonlocal engine
        if engine is None:
            engine = _get_ocr_engine()
        return _ocr_image(image, engine)

    if suffix == ".pdf":
        try:
            document = fitz.open(stream=contents, filetype="pdf")
        except Exception as exc:
            raise ExtractionError("Impossibile leggere il PDF caricato.") from exc
        with document:
            if document.page_count > MAX_PDF_PAGES:
                raise ExtractionError("Il PDF supera il limite di 30 pagine.")
            pages = []
            for page in document:
                text = page.get_text().strip()
                if len(text) < 20:
                    scale = min(2, 2200 / max(page.rect.width, page.rect.height))
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                        pixmap.height, pixmap.width, pixmap.n
                    )
                    if pixmap.n == 4:
                        image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
                    else:
                        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                    text = recognize(image)
                pages.append(text)
    else:
        image = cv2.imdecode(np.frombuffer(contents, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ExtractionError("Impossibile leggere l’immagine caricata.")
        pages = [recognize(image)]

    text = "\n\n".join(page for page in pages if page).strip()
    if not text:
        raise ExtractionError("Non è stato possibile riconoscere testo nel documento.")
    return text


def ollama_extract(text: str, model: str, instructions: str = ""):
    """Ask the local Ollama server to return extracted data as a JSON value."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", model):
        raise ExtractionError("Nome del modello Ollama non valido.")
    system_prompt = (
        "Estrai dal testo solo informazioni esplicitamente presenti. "
        "Non inventare valori; usa null quando un dato richiesto non è disponibile. "
        "Rispondi esclusivamente con un oggetto JSON valido."
    )
    user_prompt = f"Istruzioni di estrazione: {instructions or 'Estrai i dati rilevanti.'}\n\nTesto:\n{text}"
    payload = json.dumps(
        {
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = result["message"]["content"]
        structured_data = json.loads(content)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            f"Impossibile raggiungere Ollama ({OLLAMA_URL}). Verifica che il servizio sia avviato."
        ) from exc
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Ollama non ha restituito un JSON valido.") from exc
    return structured_data


def parse_upload(body: bytes, content_type: str):
    """Return uploaded bytes, filename, model and instructions from multipart form data."""
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii")
        + body
    )
    if not message.is_multipart():
        raise ExtractionError("Richiesta di caricamento non valida.")
    fields = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name == "document" and part.get_filename():
            filename = Path(part.get_filename().replace("\\", "/")).name
            contents = part.get_payload(decode=True) or b""
            fields["document"] = (contents, filename)
        elif name in {"model", "instructions"}:
            fields[name] = (part.get_payload(decode=True) or b"").decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
    if "document" not in fields:
        raise ExtractionError("Seleziona un documento da elaborare.")
    return (
        fields["document"][0],
        fields["document"][1],
        fields.get("model", "llama3.2").strip(),
        fields.get("instructions", "").strip(),
    )


class AppHandler(BaseHTTPRequestHandler):
    server_version = "EstrattorePDF/1.0"

    def do_GET(self):
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return
        page = PAGE.replace("__OLLAMA_URL__", html.escape(OLLAMA_URL, quote=True))
        payload = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if urlsplit(self.path).path != "/extract":
            self._send_json(404, {"error": "Percorso non trovato."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Dimensione della richiesta non valida."})
            return
        if length <= 0 or length > MAX_UPLOAD_BYTES + 256_000:
            self._send_json(413, {"error": "La richiesta è vuota o supera il limite di 20 MB."})
            return
        try:
            contents, filename, model, instructions = parse_upload(
                self.rfile.read(length), self.headers.get("Content-Type", "")
            )
            if len(contents) > MAX_UPLOAD_BYTES:
                raise ExtractionError("Il file supera il limite di 20 MB.")
            if len(instructions) > 2000 or len(model) > 100:
                raise ExtractionError("Le istruzioni o il nome modello sono troppo lunghi.")
            extracted_text = extract_document(contents, filename)
            structured_data = ollama_extract(extracted_text, model, instructions)
        except ExtractionError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self._send_json(502, {"error": str(exc)})
            return
        except Exception:
            self.log_error("Errore interno durante l’estrazione")
            self._send_json(500, {"error": "Errore interno durante l’estrazione."})
            return
        self._send_json(
            200,
            {
                "filename": filename,
                "extracted_text": extracted_text,
                "structured_data": structured_data,
            },
        )

    def _send_json(self, status: int, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        if os.environ.get("APP_ACCESS_LOG") == "1":
            super().log_message(format, *args)


def main():
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Estrattore Dati PDF AI disponibile su http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArresto dell’applicazione.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
