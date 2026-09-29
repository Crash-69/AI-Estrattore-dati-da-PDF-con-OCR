import json
import os
import re
import sys

# PYTHONIOENCODING vale solo per i processi figli: stdout gia' aperto va riconfigurato
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pymupdf
import ollama
from pathlib import Path
from rapidocr import RapidOCR

BASE_DIR = Path(__file__).resolve().parent
LLM_MODEL = "nemotron-3-nano:4b"
# OLLAMA_HOST=0.0.0.0 e' un indirizzo di bind, non raggiungibile come destinazione su Windows
client = ollama.Client(host="http://127.0.0.1:11434")

# Include le lettere di omocodia nelle posizioni numeriche
CF_PERSONA_RE = re.compile(r"\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b")
PIVA_RE = re.compile(r"^\d{11}$")


def _cf_code(word, is_name):
    cons = [c for c in word if c.isalpha() and c not in "AEIOU"]
    vows = [c for c in word if c in "AEIOU"]
    if is_name and len(cons) >= 4:
        cons = [cons[0], cons[2], cons[3]]
    return "".join(cons + vows + ["X"] * 3)[:3]


def _find_word_for_code(ocr_text, code, is_name):
    for word in re.findall(r"\b[A-Z']{2,}\b", ocr_text.upper()):
        if _cf_code(word, is_name) == code:
            return word
    return None


def validate_fields(data, ocr_text):
    cf_cli = str(data.get("codice_fiscale_cliente") or "").upper()
    if not CF_PERSONA_RE.fullmatch(cf_cli):
        found = CF_PERSONA_RE.findall(ocr_text.upper())
        cf_cli = found[0] if found else None
    data["codice_fiscale_cliente"] = cf_cli

    piva = str(data.get("partita_iva_fornitore") or "")
    cf_forn = str(data.get("codice_fiscale_fornitore") or "").upper()
    if not (PIVA_RE.fullmatch(cf_forn) or CF_PERSONA_RE.fullmatch(cf_forn)):
        data["codice_fiscale_fornitore"] = piva if PIVA_RE.fullmatch(piva) else None

    # Cognome e nome devono essere coerenti con i primi 6 caratteri del CF
    for key, start, is_name in (("cognome_cliente", 0, False), ("nome_cliente", 3, True)):
        value = str(data.get(key) or "").upper()
        if not cf_cli:
            data[key] = value if value.isalpha() and value not in ("COGNOME", "NOME") else None
            continue
        code = cf_cli[start:start + 3]
        data[key] = value if value and _cf_code(value, is_name) == code else _find_word_for_code(ocr_text, code, is_name)
    return data


def ocr_first_page(pdf_path):
    doc = pymupdf.open(pdf_path)
    pix = doc.load_page(0).get_pixmap(dpi=200)
    image_path = str(BASE_DIR / "page_temp.png")
    pix.save(image_path)
    doc.close()

    result = RapidOCR()(image_path)
    if not result.txts:
        return "", []
    # Ordine di lettura: per riga (y del box, tolleranza 10px) poi per x
    items = sorted(zip(result.boxes, result.txts, result.scores), key=lambda it: (round(it[0][0][1] / 10), it[0][0][0]))
    ocr_items = [{"text": txt, "score": float(score), "box": box.tolist()} for box, txt, score in items]
    return "\n".join(it["text"] for it in ocr_items), ocr_items


def save_json(path, content):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(content, f, indent=2, ensure_ascii=False)
    print(f"Salvato: {path}")


def extract_invoice_data(pdf_path):
    print("OCR locale (RapidOCR) in corso...")
    ocr_text, ocr_items = ocr_first_page(pdf_path)
    if not ocr_text:
        raise ValueError("Nessun testo riconosciuto dall'OCR")
    stem = Path(pdf_path).stem
    save_json(BASE_DIR / f"{stem}_ocr.json", {"pdf": Path(pdf_path).name, "text": ocr_text, "items": ocr_items})

    prompt = f"""
    Sei un assistente di estrazione dati. Di seguito il testo OCR di una fattura: estrai le seguenti informazioni.
    Il fornitore e' chi emette la fattura (intestazione con P.IVA); il cliente e' il destinatario/paziente.
    Regole:
    - Il codice fiscale di una persona ha 16 caratteri nel formato 6 lettere, 2 cifre, 1 lettera, 2 cifre, 1 lettera, 3 cifre, 1 lettera (es. RSSMRA80A01H501U). Scarta codici esadecimali o identificativi della marca da bollo.
    - Cognome e nome del cliente sono parole alfabetiche, mai codici.
    - Se P.IVA e C.F. del fornitore sono indicati insieme ("P.IVA/C.F."), usa lo stesso valore; il numero CC.II.AA/REA non e' un codice fiscale.
    - L'imposta di bollo NON e' IVA: se le righe sono esenti (es. "Esenti art. 10") l'iva_totale e' 0.
    - Il totale_importo e' il "Totale Documento" o "Totale a Pagare".
    Se un dato non e' presente usa null.
    Fornisci la risposta ESCLUSIVAMENTE in formato JSON valido, senza aggiungere spiegazioni o testo aggiuntivo.
    
    Struttura richiesta:
    {{
      "fornitore": "Nome Azienda",
      "partita_iva_fornitore": "PIVA",
      "data_fattura": "GG/MM/AAAA",
      "numero_fattura": "ID",
      "totale_importo": 0.00,
      "iva_totale": 0.00,
      "imponibile_totale": 0.00,
      "codice_fiscale_fornitore": "CF",
      "codice_fiscale_cliente": "CF",
      "cognome_cliente": "Cognome",
      "nome_cliente": "Nome",
      "descrizione": "Descrizione Articolo"
    }}

    Testo OCR:
    ---
    {ocr_text}
    ---
    """

    print(f"Invio del testo OCR a {LLM_MODEL} su Ollama...")
    response = client.chat(
        model=LLM_MODEL,
        messages=[{'role': 'user', 'content': prompt}],
        format='json',
        think=False,
        options={"temperature": 0.0}
    )

    data = json.loads(response['message']['content'])
    nemotron_raw = dict(data)
    validated = validate_fields(data, ocr_text)
    save_json(BASE_DIR / f"{stem}_nemotron.json", {"model": LLM_MODEL, "risposta_modello": nemotron_raw, "dati_validati": validated})
    return validated

# Esempio di utilizzo:
risultato = extract_invoice_data(str(BASE_DIR / "CCF_000094.pdf"))
print("\nDati Estratti:")
print(json.dumps(risultato, indent=2, ensure_ascii=False))
