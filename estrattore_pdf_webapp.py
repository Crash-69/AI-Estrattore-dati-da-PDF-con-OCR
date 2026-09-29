#!/usr/bin/env python3
"""
===============================================================================
Estrattore PDF & Fatture con RapidOCR + Ollama LLM - WebApp Locale
===============================================================================
Applicazione web locale in unico file Python con zero dipendenze web esterne
(usa http.server / ThreadingHTTPServer della standard library Python).
Stile visivo, layout e fogli di stile (CSS) ereditati ed allineati con SecureVault.

Funzionalità principali:
1. Scelta dinamica del modello Ollama LLM installato (tramite API locale /api/tags).
2. Gestione PDF parametrica: Toggle tra RapidOCR (scansioni/immagini) e Testo Nativo (PyMuPDF).
3. Prompt di sistema interamente parametrico ed editabile da UI.
4. Campi di estrazione personalizzabili dall'utente per la struttura del JSON di output.
5. Selezione cartella locale di Input (PDF) e Output (salvataggio automatico JSON).
6. Upload diretto via Drag & Drop di file PDF.
7. Esportazione diretta in pagina HTML formattata e stampabile.

Avvio:
    python estrattore_pdf_webapp.py
-------------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Configurazione encoding stdout/stderr per Windows
os.environ["PYTHONIOENCODING"] = "utf-8"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Importazione librerie core di elaborazione (PyMuPDF, RapidOCR, Ollama)
try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        raise ImportError("La libreria PyMuPDF (pymupdf o fitz) non e' installata nell'ambiente Python in uso.")

import ollama

try:
    from rapidocr import RapidOCR
    HAS_RAPID_OCR = True
except ImportError:
    try:
        from rapidocr_onnxruntime import RapidOCR
        HAS_RAPID_OCR = True
    except ImportError:
        HAS_RAPID_OCR = False

# Costanti di default
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8020
DEFAULT_OLLAMA_MODEL = "nemotron-3-nano:4b"
OLLAMA_CLIENT_HOST = "http://127.0.0.1:11434"

# Espressioni Regolari per Validazione Codice Fiscale e Partita IVA (inclusa omocodia)
CF_PERSONA_RE = re.compile(
    r"\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b"
)
PIVA_RE = re.compile(r"^\d{11}$")

# Campi di estrazione predefiniti per fatture
DEFAULT_FIELDS = (
    "fornitore, partita_iva_fornitore, data_fattura, numero_fattura, "
    "totale_importo, iva_totale, imponibile_totale, codice_fiscale_fornitore, "
    "codice_fiscale_cliente, cognome_cliente, nome_cliente, descrizione"
)

# Prompt di sistema predefinito
DEFAULT_PROMPT_TEMPLATE = """Sei un assistente esperto nell'estrazione e strutturazione di dati da documenti e fatture.
Analizza il seguente testo estratto da un documento/fattura ed estrai le informazioni richieste nei campi indicati.

Regole operative stringenti:
- Il fornitore e' chi emette il documento (intestazione con P.IVA); il cliente e' il destinatario/paziente.
- Il codice fiscale di una persona fisica ha 16 caratteri alfanumerici (es. RSSMRA80A01H501U). Scarta identificativi di marca da bollo o codici interni.
- Cognome e nome del cliente sono parole alfabetiche, mai codici.
- Se P.IVA e C.F. del fornitore coincidono o sono indicati insieme, usa lo stesso valore.
- L'imposta di bollo NON e' IVA: se le righe sono esenti (es. "Esenti art. 10") l'iva_totale e' 0.00.
- Il totale_importo rappresenta il "Totale Documento" o "Totale da Pagare".
- Restituisci SOLO ed ESCLUSIVAMENTE un oggetto JSON valido contenente le chiavi richieste, senza alcun testo o commento aggiuntivo.
- Se un dato non e' presente o non e' rilevabile dal testo, assegna il valore null.

Campi specifici da estrare ed includere come chiavi nel JSON:
{FIELDS_SCHEMA}

Testo estratto dal documento:
---
{EXTRACTED_TEXT}
---"""


# =============================================================================
# LOGICA DI ESTRAZIONE E VALIDAZIONE DATI
# =============================================================================

def _cf_code(word: str, is_name: bool) -> str:
    """Calcola i 3 caratteri del codice fiscale per cognome o nome."""
    cons = [c for c in word if c.isalpha() and c not in "AEIOU"]
    vows = [c for c in word if c in "AEIOU"]
    if is_name and len(cons) >= 4:
        cons = [cons[0], cons[2], cons[3]]
    return "".join(cons + vows + ["X"] * 3)[:3]


def _find_word_for_code(ocr_text: str, code: str, is_name: bool) -> str | None:
    """Cerca nel testo una parola alfabetica corrispondente al codice del CF."""
    for word in re.findall(r"\b[A-Z']{2,}\b", ocr_text.upper()):
        if _cf_code(word, is_name) == code:
            return word
    return None


def validate_fields(data: dict, raw_text: str) -> dict:
    """Valida e corregge i campi chiave estratti (Codice Fiscale, P.IVA, Nomi)."""
    if not isinstance(data, dict):
        return data

    # Validazione Codice Fiscale Cliente
    cf_cli = str(data.get("codice_fiscale_cliente") or "").upper().strip()
    if not CF_PERSONA_RE.fullmatch(cf_cli):
        found = CF_PERSONA_RE.findall(raw_text.upper())
        cf_cli = found[0] if found else None
    data["codice_fiscale_cliente"] = cf_cli

    # Validazione Partita IVA e Codice Fiscale Fornitore
    piva = str(data.get("partita_iva_fornitore") or "").strip()
    cf_forn = str(data.get("codice_fiscale_fornitore") or "").upper().strip()
    if not (PIVA_RE.fullmatch(cf_forn) or CF_PERSONA_RE.fullmatch(cf_forn)):
        data["codice_fiscale_fornitore"] = piva if PIVA_RE.fullmatch(piva) else None

    # Coerenza Cognome e Nome con il CF Cliente
    for key, start, is_name in (("cognome_cliente", 0, False), ("nome_cliente", 3, True)):
        if key not in data:
            continue
        value = str(data.get(key) or "").upper().strip()
        if not cf_cli:
            data[key] = value if value.isalpha() and value not in ("COGNOME", "NOME") else None
            continue
        code = cf_cli[start:start + 3]
        if value and _cf_code(value, is_name) == code:
            data[key] = value
        else:
            data[key] = _find_word_for_code(raw_text, code, is_name)
    return data


def get_ollama_models() -> list[str]:
    """Interroga l'istanza Ollama locale per recuperare la lista dei modelli installati."""
    default_list = [DEFAULT_OLLAMA_MODEL, "llama3:latest", "mistral:latest", "qwen2.5:latest"]
    try:
        client = ollama.Client(host=OLLAMA_CLIENT_HOST)
        models_response = client.list()
        # ollama python library returns object with .models list
        models = []
        if hasattr(models_response, 'models'):
            for m in models_response.models:
                name = getattr(m, 'model', None) or getattr(m, 'name', None)
                if name:
                    models.append(name)
        elif isinstance(models_response, dict) and "models" in models_response:
            for m in models_response["models"]:
                if isinstance(m, dict) and "name" in m:
                    models.append(m["name"])
        if models:
            return sorted(list(set(models)))
    except Exception as e:
        print(f"[WARN] Impossibile recuperare lista modelli Ollama: {e}")
    return default_list


def extract_pdf_text_native(pdf_path: str) -> str:
    """Estrae direttamente il testo nativo da tutte le pagine del PDF senza OCR."""
    doc = pymupdf.open(pdf_path)
    full_text = []
    for page_idx in range(len(doc)):
        page = doc.load_page(page_idx)
        text = page.get_text("text")
        if text.strip():
            full_text.append(f"--- Pagina {page_idx + 1} ---\n" + text.strip())
    doc.close()
    return "\n\n".join(full_text)


def extract_pdf_text_ocr(pdf_path: str) -> tuple[str, list]:
    """Esegue OCR (RapidOCR) sulla prima pagina del PDF (convertita in immagine)."""
    if not HAS_RAPID_OCR:
        raise ImportError("La libreria 'rapidocr' non e' installata nel sistema.")

    doc = pymupdf.open(pdf_path)
    if len(doc) == 0:
        doc.close()
        return "", []

    pix = doc.load_page(0).get_pixmap(dpi=200)
    tmp_dir = tempfile.gettempdir()
    tmp_img_path = os.path.join(tmp_dir, f"ocr_page_temp_{int(time.time()*1000)}.png")
    pix.save(tmp_img_path)
    doc.close()

    try:
        engine = RapidOCR()
        result = engine(tmp_img_path)
        if not result or not getattr(result, 'txts', None):
            return "", []

        # Ordine di lettura per riga poi colonna
        items = sorted(
            zip(result.boxes, result.txts, result.scores),
            key=lambda it: (round(it[0][0][1] / 10), it[0][0][0])
        )
        ocr_items = [{"text": txt, "score": float(score), "box": box.tolist()} for box, txt, score in items]
        ocr_text = "\n".join(it["text"] for it in ocr_items)
        return ocr_text, ocr_items
    finally:
        if os.path.exists(tmp_img_path):
            os.remove(tmp_img_path)


def process_extraction(
    pdf_path: str,
    mode: str,
    model_name: str,
    custom_prompt: str,
    fields_list_str: str,
    output_dir: str
) -> dict:
    """Coordina l'intero flusso di estrazione: lettura PDF, invio ad Ollama e salvataggio JSON."""
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        raise FileNotFoundError(f"File PDF non trovato: {pdf_path}")

    print(f"\n[INFO] [{time.strftime('%H:%M:%S')}] Avvio estrazione per PDF: {pdf_file.name}", flush=True)

    # 1. Estrazione testo (OCR o Nativo)
    ocr_items = []
    if mode == "ocr":
        print(f"[INFO] 🔍 Esecuzione OCR locale (RapidOCR)...", flush=True)
        ocr_text, ocr_items = extract_pdf_text_ocr(str(pdf_file))
        if not ocr_text.strip():
            print(f"[WARN] RapidOCR non ha trovato testo. Tentativo estrazione nativa...", flush=True)
            ocr_text = extract_pdf_text_native(str(pdf_file))
    else:
        print(f"[INFO] 📄 Estrazione diretta testo nativo (PyMuPDF)...", flush=True)
        ocr_text = extract_pdf_text_native(str(pdf_file))
        if not ocr_text.strip() and HAS_RAPID_OCR:
            print(f"[WARN] Testo nativo vuoto. Tentativo estrazione OCR...", flush=True)
            ocr_text, ocr_items = extract_pdf_text_ocr(str(pdf_file))

    if not ocr_text.strip():
        raise ValueError("Impossibile estrarre alcun testo dal documento PDF fornito.")

    print(f"[INFO] 📝 Testo estratto ({len(ocr_text)} caratteri). Preparazione prompt per Ollama...", flush=True)

    # 2. Costruzione dello Schema dei Campi Richiesti
    fields = [f.strip() for f in fields_list_str.split(",") if f.strip()]
    if not fields:
        fields = [f.strip() for f in DEFAULT_FIELDS.split(",")]

    fields_schema_dict = {field: f"Valore per {field}" for field in fields}
    fields_schema_json = json.dumps(fields_schema_dict, indent=2, ensure_ascii=False)

    # 3. Assemblaggio Prompt Personalizzato
    prompt = custom_prompt.replace("{FIELDS_SCHEMA}", fields_schema_json).replace("{EXTRACTED_TEXT}", ocr_text)

    # 4. Richiesta ad Ollama LLM
    print(f"[INFO] 🤖 Invio del testo a Ollama (Modello: '{model_name}')...", flush=True)
    t_start = time.time()
    
    try:
        client = ollama.Client(host=OLLAMA_CLIENT_HOST)
        response = client.chat(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            format="json",
            options={"temperature": 0.0}
        )
    except Exception as err:
        print(f"[ERROR] Errore durante la chiamata ad Ollama: {err}", flush=True)
        raise RuntimeError(f"Errore durante l'elaborazione con il modello '{model_name}': {err}")

    elapsed = round(time.time() - t_start, 2)
    print(f"[INFO] ✅ Risposta ricevuta da Ollama in {elapsed} secondi!", flush=True)

    raw_response_str = response["message"]["content"]
    data = json.loads(raw_response_str)

    # 5. Validazione euristica dati
    validated_data = validate_fields(data, ocr_text)

    # 6. Determinazione directory di Output (Default: C:\Progetto_AI\Estrae_testo_da_Pdf_richiama_Ollama)
    default_fallback_dir = Path(r"C:\Progetto_AI\Estrae_testo_da_Pdf_richiama_Ollama")
    if output_dir and output_dir.strip():
        out_dir_path = Path(output_dir.strip())
    else:
        out_dir_path = default_fallback_dir

    out_dir_path.mkdir(parents=True, exist_ok=True)
    stem = pdf_file.stem

    # 7. Salvataggio automatico del JSON OCR Grezzo
    json_ocr_path = out_dir_path / f"{stem}_ocr.json"
    ocr_payload = {
        "pdf": pdf_file.name,
        "modalita": mode,
        "text": ocr_text,
        "items": ocr_items
    }
    with open(json_ocr_path, "w", encoding="utf-8") as f_ocr:
        json.dump(ocr_payload, f_ocr, indent=2, ensure_ascii=False)
    print(f"[INFO] 💾 Salvato JSON OCR Grezzo in: {json_ocr_path}", flush=True)

    # 8. Salvataggio automatico del JSON Dati Estratti LLM
    json_output_path = out_dir_path / f"{stem}_estratto.json"
    result_payload = {
        "file_pdf": pdf_file.name,
        "modalita_estrazione": mode,
        "modello_llm": model_name,
        "campi_richiesti": fields,
        "dati_estratti": validated_data,
        "testo_grezzo": ocr_text,
        "ocr_payload": ocr_payload,
        "salvato_in": str(json_output_path),
        "salvato_ocr_in": str(json_ocr_path)
    }

    with open(json_output_path, "w", encoding="utf-8") as f_estratto:
        json.dump(result_payload, f_estratto, indent=2, ensure_ascii=False)
    print(f"[INFO] 💾 Salvato JSON Dati Estratti in: {json_output_path}", flush=True)

    return result_payload


def build_html_formatted(data_dict: dict) -> str:
    """Genera un documento HTML responsive e formattato stile scheda fattura/tabella stampabile."""
    pdf_name = data_dict.get("file_pdf", "Documento")
    model_used = data_dict.get("modello_llm", "N/A")
    mode_used = data_dict.get("modalita_estrazione", "N/A")
    dati = data_dict.get("dati_estratti", {})

    rows_html = ""
    for k, v in dati.items():
        key_label = k.replace("_", " ").title()
        val_str = "<i>null</i>" if v is None else str(v)
        rows_html += f"""
        <tr>
            <td class="key-col">{key_label}</td>
            <td class="val-col">{val_str}</td>
        </tr>
        """

    html = f"""<!doctype html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Report Estrazione - {pdf_name}</title>
<style>
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        background-color: #f8fafc;
        color: #1e293b;
        margin: 0;
        padding: 40px 20px;
    }}
    .container {{
        max-width: 800px;
        margin: 0 auto;
        background: #ffffff;
        border-radius: 20px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05);
        border: 1px solid #e2e8f0;
        overflow: hidden;
    }}
    .header {{
        background: #0f172a;
        color: #ffffff;
        padding: 32px 40px;
    }}
    .header h1 {{
        margin: 0 0 8px 0;
        font-size: 26px;
        font-weight: 600;
        letter-spacing: -0.02em;
    }}
    .header .meta {{
        color: #94a3b8;
        font-size: 14px;
        display: flex;
        gap: 20px;
        flex-wrap: wrap;
    }}
    .content {{
        padding: 40px;
    }}
    .table-container {{
        width: 100%;
        border-collapse: collapse;
        margin-top: 10px;
    }}
    th, td {{
        text-align: left;
        padding: 14px 18px;
        border-bottom: 1px solid #edf2f7;
    }}
    th {{
        background: #f1f5f9;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #64748b;
    }}
    .key-col {{
        font-weight: 600;
        color: #334155;
        width: 40%;
        background-color: #f8fafc;
    }}
    .val-col {{
        color: #0f172a;
    }}
    .footer {{
        background: #f8fafc;
        padding: 20px 40px;
        border-top: 1px solid #e2e8f0;
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 13px;
        color: #64748b;
    }}
    .btn-print {{
        background: #059669;
        color: white;
        border: none;
        padding: 10px 18px;
        border-radius: 10px;
        font-weight: 600;
        cursor: pointer;
    }}
    .btn-print:hover {{
        background: #047857;
    }}
    @media print {{
        .btn-print {{ display: none; }}
        body {{ padding: 0; background: white; }}
        .container {{ box-shadow: none; border: none; }}
    }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>Scheda Dati Estratti Fattura</h1>
        <div class="meta">
            <span>📄 File: <strong>{pdf_name}</strong></span>
            <span>🤖 Modello LLM: <strong>{model_used}</strong></span>
            <span>⚙️ Modalità: <strong>{mode_used.upper()}</strong></span>
        </div>
    </div>
    <div class="content">
        <table class="table-container">
            <thead>
                <tr>
                    <th>Campo Estratto</th>
                    <th>Valore Rilevato</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
    <div class="footer">
        <span>Generato automaticamente tramite Estrattore AI Locale</span>
        <button class="btn-print" onclick="window.print()">🖨️ Stampa / Salva PDF</button>
    </div>
</div>
</body>
</html>"""
    return html


# =============================================================================
# INTERFACCIA WEB (HTTP SERVER EMBEDDED - STILE SECUREVAULT)
# =============================================================================

HTML_PAGE = """<!doctype html>
<html lang="it">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Estrattore Dati PDF & Fatture AI</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; background: #f5f5f5; color: #1a1a1a;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    display: flex; justify-content: center;
  }
  .wrap { max-width: 780px; width: 100%; padding: 48px 24px; }
  .header { text-align: center; margin-bottom: 36px; }
  .logo {
    width: 64px; height: 64px; margin: 0 auto 18px; border-radius: 18px;
    background: #fff; border: 1px solid rgba(0,0,0,0.06); box-shadow: 0 4px 12px rgba(0,0,0,0.04);
    display: flex; align-items: center; justify-content: center; font-size: 32px;
  }
  h1 { font-size: 32px; font-weight: 700; margin: 0 0 8px; letter-spacing: -0.02em; color: #0f172a; }
  .subtitle { color: #64748b; font-size: 16px; margin: 0; }
  
  .card {
    background: #fff; border-radius: 24px; padding: 32px; margin-bottom: 24px;
    border: 1px solid rgba(0,0,0,0.06); box-shadow: 0 2px 12px rgba(0,0,0,0.03);
  }
  
  .card-title { font-size: 18px; font-weight: 600; margin: 0 0 16px; color: #0f172a; display: flex; align-items: center; gap: 8px; }
  
  .field { margin-bottom: 22px; }
  .field:last-child { margin-bottom: 0; }
  
  label { display: block; font-size: 12px; font-weight: 600; color: #64748b;
    text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px; }
    
  input[type="text"], select, textarea {
    width: 100%; background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 12px; padding: 13px 16px; font-size: 15px; outline: none;
    font-family: inherit; color: #0f172a; transition: all 0.2s;
  }
  textarea { resize: vertical; min-height: 120px; line-height: 1.5; font-size: 13px; font-family: monospace; }
  input:focus, select:focus, textarea:focus { border-color: #059669; box-shadow: 0 0 0 3px rgba(5,150,105,0.15); background: #fff; }
  
  .row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  
  .radio-group { display: flex; gap: 12px; }
  .radio-card {
    flex: 1; border: 1px solid #e2e8f0; background: #f8fafc; border-radius: 12px;
    padding: 12px 16px; cursor: pointer; display: flex; align-items: center; gap: 10px;
    font-size: 14px; font-weight: 500; transition: all 0.2s;
  }
  .radio-card input { accent-color: #059669; }
  .radio-card.selected { border-color: #059669; background: rgba(5,150,105,0.05); color: #047857; }
  
  .dropzone {
    border: 2px dashed #cbd5e1; border-radius: 18px; padding: 36px 20px;
    text-align: center; cursor: pointer; transition: all 0.2s; background: #f8fafc;
  }
  .dropzone:hover, .dropzone.dragging { border-color: #059669; background: rgba(5,150,105,0.04); }
  .dropzone.has-file { border-color: #059669; background: rgba(5,150,105,0.03); }
  .dz-icon { font-size: 36px; margin-bottom: 8px; }
  .dz-title { font-weight: 600; font-size: 16px; margin: 0 0 4px; color: #0f172a; }
  .dz-hint { color: #64748b; font-size: 13px; margin: 0; }
  
  .action-btn {
    width: 100%; margin-top: 8px; padding: 16px; border: none; border-radius: 16px;
    font-weight: 600; font-size: 17px; cursor: pointer; display: flex; align-items: center;
    justify-content: center; gap: 10px; transition: all 0.15s; color: #fff; background: #059669;
    box-shadow: 0 8px 20px rgba(5,150,105,0.2);
  }
  .action-btn:disabled { background: #cbd5e1; color: #94a3b8; box-shadow: none; cursor: not-allowed; }
  .action-btn:not(:disabled):hover { background: #047857; }
  .action-btn:not(:disabled):active { transform: scale(0.98); }
  
  .btn-secondary {
    background: #0f172a; box-shadow: 0 4px 12px rgba(15,23,42,0.15);
    padding: 10px 18px; font-size: 14px; border-radius: 10px; width: auto; color: #fff; border: none; cursor: pointer;
  }
  .btn-secondary:hover { background: #1e293b; }
  
  .error-box {
    margin-top: 16px; padding: 14px 16px; background: #fef2f2; border: 1px solid #fee2e2;
    border-radius: 12px; color: #dc2626; font-size: 14px; display: flex; gap: 10px; align-items: center;
  }
  
  .results-box {
    margin-top: 24px; padding: 24px; background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 16px;
  }
  
  .data-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
  .data-table td, .data-table th { padding: 10px 12px; border-bottom: 1px solid #e2e8f0; text-align: left; }
  .data-table th { background: #edf2f7; color: #475569; font-size: 11px; text-transform: uppercase; }
  .data-table td.key { font-weight: 600; color: #334155; width: 35%; }
  
  .footer { margin-top: 40px; text-align: center; }
  .badges { display: flex; justify-content: center; gap: 20px; color: #64748b; margin-bottom: 12px; }
  .badge { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
  .dot { width: 8px; height: 8px; border-radius: 999px; background: #10b981; }
  .hidden { display: none !important; }
  .spin { animation: spin 0.8s linear infinite; display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">📄🤖</div>
    <h1>Estrattore Dati PDF AI</h1>
    <p class="subtitle">Estrazione automatica e strutturazione JSON con RapidOCR & Ollama LLM</p>
  </div>

  <div class="card">
    <div class="card-title">⚙️ Parametri Modello & PDF</div>
    
    <div class="row-2">
      <div class="field">
        <label>Modello Ollama LLM</label>
        <select id="modelSelect">
          <option value="nemotron-3-nano:4b">Caricamento modelli in corso...</option>
        </select>
      </div>
      <div class="field">
        <label>Modalità Lettura PDF</label>
        <div class="radio-group">
          <label class="radio-card selected" id="lblOcr">
            <input type="radio" name="pdfMode" value="ocr" checked /> RapidOCR (Immagini)
          </label>
          <label class="radio-card" id="lblNative">
            <input type="radio" name="pdfMode" value="native" /> Testo Nativo (Veloce)
          </label>
        </div>
      </div>
    </div>

    <div class="field">
      <label>Campi Specifici da Estrarre (Separati da Virgola)</label>
      <input type="text" id="fieldsInput" />
    </div>

    <div class="field">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <label style="margin:0;">Prompt di Sistema / Istruzioni LLM</label>
        <span style="font-size:12px; color:#059669; cursor:pointer; font-weight:600;" id="resetPromptBtn">🔄 Ripristina Default</span>
      </div>
      <textarea id="promptInput"></textarea>
    </div>
  </div>

  <div class="card">
    <div class="card-title">📂 Sorgente & Destinazione Local Path</div>
    
    <div class="field">
      <label>Directory Input PDF (Opzionale se usi Upload sotto)</label>
      <input type="text" id="inputDir" placeholder="Es. C:\\Progetto_AI\\Estrae_testo_da_Pdf_richiama_Ollama" />
    </div>

    <div class="field">
      <label>Directory Output Salvataggio JSON (Opzionale)</label>
      <input type="text" id="outputDir" placeholder="Es. C:\\Progetto_AI\\Estrae_testo_da_Pdf_richiama_Ollama" />
    </div>

    <div id="dropzone" class="dropzone">
      <input id="fileInput" type="file" class="hidden" accept=".pdf" />
      <div id="uploadPrompt">
        <div class="dz-icon">📥</div>
        <p class="dz-title">Trascina un file PDF qui oppure Clicca per Sfogliare</p>
        <p class="dz-hint" id="fileSelectedHint">Nessun file selezionato</p>
      </div>
    </div>

    <button id="extractBtn" class="action-btn" style="margin-top:20px;">
      <span id="btnLabel">⚡ Avvia Estrazione Dati</span>
    </button>

    <div id="errorBox" class="error-box hidden">
      <span>⚠️</span>
      <p id="errorText" style="margin:0;"></p>
    </div>
  </div>

  <!-- Scheda Risultati -->
  <div id="resultsCard" class="card hidden">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:10px;">
      <div class="card-title" style="margin:0;">📊 Risultati Estrazione</div>
      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <button id="downloadOcrJsonBtn" class="btn-secondary" style="background:#475569;">📄 Scarica JSON OCR</button>
        <button id="downloadJsonBtn" class="btn-secondary">📥 Scarica JSON Estratto</button>
        <button id="downloadHtmlBtn" class="btn-secondary" style="background:#059669;">🌐 Scarica HTML Formattato</button>
      </div>
    </div>
    
    <div id="savedPathInfo" style="background:#f1f5f9; border:1px solid #cbd5e1; border-radius:12px; padding:14px; font-size:13px; color:#334155; margin-bottom:18px; line-height:1.6;"></div>
    
    <table class="data-table" id="extractedTable">
      <thead>
        <tr><th>Campo</th><th>Valore Estratto</th></tr>
      </thead>
      <tbody></tbody>
    </table>

    <details style="margin-top:20px;">
      <summary style="font-weight:600; font-size:14px; cursor:pointer; color:#0f172a;">🔍 Visualizza JSON Dati Estratti</summary>
      <pre id="jsonPre" style="background:#0f172a; color:#38bdf8; padding:16px; border-radius:12px; font-size:12px; overflow-x:auto; margin-top:10px;"></pre>
    </details>

    <details style="margin-top:10px;">
      <summary style="font-weight:600; font-size:14px; cursor:pointer; color:#0f172a;">📄 Visualizza JSON OCR Grezzo</summary>
      <pre id="jsonOcrPre" style="background:#0f172a; color:#a7f3d0; padding:16px; border-radius:12px; font-size:12px; overflow-x:auto; margin-top:10px;"></pre>
    </details>
  </div>

  <div class="footer">
    <div class="badges">
      <div class="badge"><span class="dot"></span> RapidOCR + PyMuPDF</div>
      <div class="badge"><span class="dot"></span> Ollama LLM Local</div>
      <div class="badge"><span class="dot"></span> 100% Elaborazione Locale</div>
    </div>
  </div>
</div>

<script>
const DEFAULT_FIELDS = "{DEFAULT_FIELDS_JS}";
const DEFAULT_PROMPT = {DEFAULT_PROMPT_JS};
const API_BASE = window.location.protocol.startsWith('http') ? '' : 'http://127.0.0.1:8020';

let currentResultData = null;
let selectedFile = null;

const modelSelect = document.getElementById('modelSelect');
const fieldsInput = document.getElementById('fieldsInput');
const promptInput = document.getElementById('promptInput');
const resetPromptBtn = document.getElementById('resetPromptBtn');
const inputDir = document.getElementById('inputDir');
const outputDir = document.getElementById('outputDir');
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const fileSelectedHint = document.getElementById('fileSelectedHint');
const extractBtn = document.getElementById('extractBtn');
const btnLabel = document.getElementById('btnLabel');
const errorBox = document.getElementById('errorBox');
const errorText = document.getElementById('errorText');
const resultsCard = document.getElementById('resultsCard');
const extractedTable = document.getElementById('extractedTable').querySelector('tbody');
const jsonPre = document.getElementById('jsonPre');
const jsonOcrPre = document.getElementById('jsonOcrPre');
const savedPathInfo = document.getElementById('savedPathInfo');
const downloadJsonBtn = document.getElementById('downloadJsonBtn');
const downloadOcrJsonBtn = document.getElementById('downloadOcrJsonBtn');
const downloadHtmlBtn = document.getElementById('downloadHtmlBtn');
const lblOcr = document.getElementById('lblOcr');
const lblNative = document.getElementById('lblNative');

// Inizializzazione Valori Form
fieldsInput.value = DEFAULT_FIELDS;
promptInput.value = DEFAULT_PROMPT;

resetPromptBtn.addEventListener('click', () => {
  promptInput.value = DEFAULT_PROMPT;
});

// Toggle Modalità PDF
document.querySelectorAll('input[name="pdfMode"]').forEach(radio => {
  radio.addEventListener('change', (e) => {
    lblOcr.classList.toggle('selected', e.target.value === 'ocr');
    lblNative.classList.toggle('selected', e.target.value === 'native');
  });
});

// Caricamento modelli Ollama al boot
async function loadOllamaModels() {
  try {
    const res = await fetch(API_BASE + '/api/models');
    if (res.ok) {
      const data = await res.json();
      if (data.models && data.models.length > 0) {
        modelSelect.innerHTML = data.models.map(m => `<option value="${m}">${m}</option>`).join('');
        if (data.models.includes('nemotron-3-nano:4b')) {
          modelSelect.value = 'nemotron-3-nano:4b';
        }
      }
    }
  } catch (err) {
    console.warn("Impossibile caricare i modelli Ollama:", err);
  }
}
loadOllamaModels();

// Dropzone file handling
dropzone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', (e) => {
  if (e.target.files && e.target.files[0]) {
    selectedFile = e.target.files[0];
    fileSelectedHint.textContent = `Selezionato: ${selectedFile.name} (${(selectedFile.size / 1024).toFixed(1)} KB)`;
    dropzone.classList.add('has-file');
  }
});
dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragging'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragging'));
dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('dragging');
  if (e.dataTransfer.files && e.dataTransfer.files[0]) {
    selectedFile = e.dataTransfer.files[0];
    fileSelectedHint.textContent = `Selezionato: ${selectedFile.name} (${(selectedFile.size / 1024).toFixed(1)} KB)`;
    dropzone.classList.add('has-file');
  }
});

function showError(msg) {
  errorText.textContent = msg;
  errorBox.classList.remove('hidden');
}
function clearError() {
  errorBox.classList.add('hidden');
}

// Avvio Estrazione
extractBtn.addEventListener('click', async () => {
  clearError();
  const pdfMode = document.querySelector('input[name="pdfMode"]:checked').value;
  const modelName = modelSelect.value;
  const fields = fieldsInput.value;
  const prompt = promptInput.value;
  const inDir = inputDir.value.trim();
  const outDir = outputDir.value.trim();

  if (!selectedFile && !inDir) {
    showError("Seleziona un file PDF oppure specifica una Directory di Input.");
    return;
  }

  extractBtn.disabled = true;
  btnLabel.innerHTML = '<span class="spin">⏳</span> Elaborazione in corso...';

  try {
    const formData = new FormData();
    formData.append('pdf_mode', pdfMode);
    formData.append('model_name', modelName);
    formData.append('fields_list', fields);
    formData.append('custom_prompt', prompt);
    formData.append('input_dir', inDir);
    formData.append('output_dir', outDir);

    if (selectedFile) {
      formData.append('file', selectedFile);
    }

    const res = await fetch(API_BASE + '/api/extract', { method: 'POST', body: formData });
    if (!res.ok) {
      const errTxt = await res.text();
      throw new Error(errTxt || "Errore durante l'estrazione dati.");
    }

    const result = await res.json();
    currentResultData = result;
    renderResults(result);
  } catch (err) {
    showError(err.message);
  } finally {
    extractBtn.disabled = false;
    btnLabel.innerHTML = '⚡ Avvia Estrazione Dati';
  }
});

function renderResults(res) {
  resultsCard.classList.remove('hidden');
  
  savedPathInfo.innerHTML = `
    <strong>💾 File salvati con successo su disco:</strong><br/>
    📄 <strong>JSON OCR Grezzo:</strong> <code>${res.salvato_ocr_in || 'N/A'}</code><br/>
    🤖 <strong>JSON Dati Estratti (LLM):</strong> <code>${res.salvato_in || 'N/A'}</code>
  `;
  
  const dati = res.dati_estratti || {};
  extractedTable.innerHTML = Object.entries(dati).map(([k, v]) => {
    const keyStr = k.replace(/_/g, ' ').toUpperCase();
    const valStr = v === null ? '<i style="color:#94a3b8;">null</i>' : v;
    return `<tr><td class="key">${keyStr}</td><td>${valStr}</td></tr>`;
  }).join('');

  jsonPre.textContent = JSON.stringify(res, null, 2);
  jsonOcrPre.textContent = JSON.stringify(res.ocr_payload || {}, null, 2);
  resultsCard.scrollIntoView({ behavior: 'smooth' });
}

// Download JSON OCR Grezzo Button
downloadOcrJsonBtn.addEventListener('click', () => {
  if (!currentResultData || !currentResultData.ocr_payload) return;
  const blob = new Blob([JSON.stringify(currentResultData.ocr_payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${currentResultData.file_pdf.replace('.pdf', '')}_ocr.json`;
  a.click();
  URL.revokeObjectURL(url);
});

// Download JSON Estratto Button
downloadJsonBtn.addEventListener('click', () => {
  if (!currentResultData) return;
  const blob = new Blob([JSON.stringify(currentResultData, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${currentResultData.file_pdf.replace('.pdf', '')}_estratto.json`;
  a.click();
  URL.revokeObjectURL(url);
});

// Download HTML Formattato Button
downloadHtmlBtn.addEventListener('click', async () => {
  if (!currentResultData) return;
  try {
    const res = await fetch(API_BASE + '/api/export-html', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(currentResultData)
    });
    if (!res.ok) throw new Error("Errore generazione HTML");
    const htmlText = await res.text();
    const blob = new Blob([htmlText], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${currentResultData.file_pdf.replace('.pdf', '')}_report.html`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(err.message);
  }
});
</script>
</body>
</html>"""


class ExtractorHTTPRequestHandler(BaseHTTPRequestHandler):
    """Handler HTTP per gestire il routing della WebApp e delle API REST."""

    def log_message(self, format, *args):
        # Silenzia i log HTTP standard per mantenere pulita la console
        return

    def _send_cors_headers(self, response_code=200, content_type="application/json; charset=utf-8", length=0):
        self.send_response(response_code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Type", content_type)
        if length > 0:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_OPTIONS(self):
        self._send_cors_headers(200, "text/plain", 0)

    def do_GET(self):
        url_parsed = urllib.parse.urlparse(self.path)
        if url_parsed.path == "/" or url_parsed.path == "/index.html":
            # Iniezione valori JS predefiniti
            rendered_html = HTML_PAGE.replace("{DEFAULT_FIELDS_JS}", DEFAULT_FIELDS).replace(
                "{DEFAULT_PROMPT_JS}", json.dumps(DEFAULT_PROMPT_TEMPLATE)
            )
            self._send_cors_headers(200, "text/html; charset=utf-8", len(rendered_html.encode("utf-8")))
            self.wfile.write(rendered_html.encode("utf-8"))

        elif url_parsed.path == "/api/models":
            models = get_ollama_models()
            payload = json.dumps({"models": models}).encode("utf-8")
            self._send_cors_headers(200, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)

        else:
            self._send_cors_headers(404, "text/plain; charset=utf-8", 0)

    def do_POST(self):
        url_parsed = urllib.parse.urlparse(self.path)

        if url_parsed.path == "/api/extract":
            pdf_temp_path = None
            try:
                content_type = self.headers.get("Content-Type", "")
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)

                fields = {}

                if "multipart/form-data" in content_type:
                    boundary_bytes = b""
                    if "boundary=" in content_type:
                        b_str = content_type.split("boundary=", 1)[1].split(";")[0].strip().strip('"').strip("'")
                        boundary_bytes = b_str.encode("utf-8")
                    
                    if not boundary_bytes:
                        raise ValueError("Impossibile estrarre il boundary multipart dal Content-Type.")

                    parts = body.split(b"--" + boundary_bytes)

                    for part in parts:
                        if not part or part.startswith(b"--"):
                            continue
                        header_end = part.find(b"\r\n\r\n")
                        if header_end == -1:
                            continue
                        raw_headers = part[:header_end].decode("utf-8", "ignore")
                        content = part[header_end + 4 :].rstrip(b"\r\n")

                        disp_match = re.search(r'name="([^"]+)"', raw_headers)
                        if not disp_match:
                            continue
                        field_name = disp_match.group(1)

                        filename_match = re.search(r'filename="([^"]+)"', raw_headers)
                        if filename_match and len(content) > 0:
                            filename = filename_match.group(1)
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
                                tmp_pdf.write(content)
                                pdf_temp_path = tmp_pdf.name
                                fields["uploaded_filename"] = filename
                        else:
                            fields[field_name] = content.decode("utf-8", "ignore")
                else:
                    fields = json.loads(body.decode("utf-8"))

                # Verifica sorgente PDF
                target_pdf = pdf_temp_path
                input_dir = fields.get("input_dir", "").strip()

                if not target_pdf and input_dir:
                    in_path = Path(input_dir)
                    if in_path.is_dir():
                        pdfs = list(in_path.glob("*.pdf"))
                        if pdfs:
                            target_pdf = str(pdfs[0])
                        else:
                            raise FileNotFoundError(f"Nessun file PDF trovato nella cartella: {input_dir}")
                    elif in_path.is_file() and in_path.suffix.lower() == ".pdf":
                        target_pdf = str(in_path)

                if not target_pdf:
                    raise ValueError("Nessun file PDF fornito via Upload o presente nella Directory Input.")

                # Parametri estrazione
                pdf_mode = fields.get("pdf_mode", "ocr")
                model_name = fields.get("model_name", DEFAULT_OLLAMA_MODEL)
                custom_prompt = fields.get("custom_prompt", DEFAULT_PROMPT_TEMPLATE)
                fields_list = fields.get("fields_list", DEFAULT_FIELDS)
                output_dir = fields.get("output_dir", str(BASE_DIR))

                result = process_extraction(
                    pdf_path=target_pdf,
                    mode=pdf_mode,
                    model_name=model_name,
                    custom_prompt=custom_prompt,
                    fields_list_str=fields_list,
                    output_dir=output_dir
                )

                if "uploaded_filename" in fields:
                    result["file_pdf"] = fields["uploaded_filename"]

                res_bytes = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self._send_cors_headers(200, "application/json; charset=utf-8", len(res_bytes))
                self.wfile.write(res_bytes)

            except Exception as e:
                import traceback
                print(f"[ERROR] Errore in /api/extract: {e}", flush=True)
                traceback.print_exc()
                err_msg = str(e).encode("utf-8")
                self._send_cors_headers(500, "text/plain; charset=utf-8", len(err_msg))
                self.wfile.write(err_msg)

            finally:
                if pdf_temp_path and os.path.exists(pdf_temp_path):
                    try:
                        os.remove(pdf_temp_path)
                    except OSError:
                        pass

        elif url_parsed.path == "/api/export-html":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                data_dict = json.loads(body.decode("utf-8"))

                html_content = build_html_formatted(data_dict)
                html_bytes = html_content.encode("utf-8")

                self._send_cors_headers(200, "text/html; charset=utf-8", len(html_bytes))
                self.wfile.write(html_bytes)

            except Exception as e:
                import traceback
                print(f"[ERROR] Errore in /api/export-html: {e}", flush=True)
                traceback.print_exc()
                err_msg = str(e).encode("utf-8")
                self._send_cors_headers(500, "text/plain; charset=utf-8", len(err_msg))
                self.wfile.write(err_msg)
        else:
            self._send_cors_headers(404, "text/plain; charset=utf-8", 0)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    """Avvia il server HTTP multithread locale e apre la pagina nel browser."""
    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, ExtractorHTTPRequestHandler)
    url = f"http://{host}:{port}/"
    print(f"\n=======================================================")
    print(f"🚀 Estrattore PDF AI - WebApp Locale Avviata!")
    print(f"🌐 Indirizzo Web: {url}")
    print(f"=======================================================\n")

    # Apertura automatica nel browser predefinito dell'utente
    threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArresto del server locale...")
        httpd.server_close()


if __name__ == "__main__":
    run_server()
