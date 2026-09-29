# Documentazione Tecnica & Istruzioni d'Uso - WebApp Estrattore PDF AI

## 📌 Panoramica del Progetto

L'applicazione **Estrattore Dati PDF AI** è una webapp locale completa sviluppata in un unico file Python modulare ([estrattore_pdf_webapp.py](file:///C:/Progetto_AI/Estrae_testo_da_Pdf_richiama_Ollama/estrattore_pdf_webapp.py)). 

La soluzione integra **RapidOCR** (per la lettura OCR di scansioni/immagini) ed **Ollama LLM** (per la strutturazione semantica dei dati in formato JSON), ereditando integralmente lo stile visivo, il layout, i fogli di stile (CSS) e le scelte architetturali della webapp esistente **SecureVault** (`C:\Progetto_AI\SecureVault`).

---

## 🌟 Caratteristiche dell'Applicazione Web

1. **Stile Visivo e Layout (SecureVault)**:
   - Sviluppata in un unico file Python **senza dipendenze web esterne** (sfruttando il server HTTP nativo multithread `http.server.ThreadingHTTPServer`).
   - Interfaccia responsive, moderna ed elegante con card bianche su fondo neutro, angoli arrotondati (`border-radius: 24px`), ombre morbide (`box-shadow`), sfumature verdi (#059669 / #10b981) e badge di stato dinamici.

2. **Selezione Dinamica del Modello Ollama LLM**:
   - Menu a tendina che interroga automaticamente l'istanza Ollama locale via API (`/api/models`) e popola in tempo reale tutti i modelli installati sul tuo sistema (es. `nemotron-3-nano:4b`, `llama3`, `qwen`, `gemma`, `deepseek`, etc.).

3. **Gestione PDF Parametrica**:
   - **RapidOCR (Immagini/Scansioni)**: converte la pagina in un rendering ad alta definizione a 200 DPI ed applica l'OCR via `RapidOCR`.
   - **Testo Nativo (PyMuPDF / fitz)**: per un'estrazione diretta ed istantanea del testo dai PDF digitali senza passare dall'OCR.

4. **Prompt & Campi di Estrazione Parametrici**:
   - **Campi personalizzabili**: campo di testo in cui l'utente può specificare liberamente quali valori estrarre (es. `fornitore, partita_iva_fornitore, data_fattura, numero_fattura, totale_importo...`). Lo schema JSON richiesto viene generato ed iniettato nel prompt in tempo reale.
   - **Prompt Editabile**: area di testo (TextArea) in cui è possibile personalizzare le istruzioni inviate all'LLM o ripristinare quelle predefinite tramite il pulsante "🔄 Ripristina Default".

5. **Selezione Directory di Input e Output con Doppio Salvataggio JSON**:
   - **Salvataggio Automatico JSON OCR Grezzo**: salva automaticamente il file `<nome_pdf>_ocr.json` contenente il testo estratto e le coordinate/box OCR.
   - **Salvataggio Automatico JSON Dati Estratti LLM**: salva automaticamente il file `<nome_pdf>_estratto.json` contenente i dati strutturati e validati.
   - **Directory di Output Configurabile**: se non specificata dall'utente, utilizza di default la directory `C:\Progetto_AI\Estrae_testo_da_Pdf_richiama_Ollama`.
   - Evidenza visiva nell'interfaccia con box contenente i percorsi completi dei file salvati su disco e pulsanti dedicati per scaricare sia il JSON OCR sia il JSON Estratto.

6. **Esportazione HTML Formattato**:
   - Generazione di una scheda/tabella elegante e stampabile con i dati estratti dal documento.
   - Pulsante **"🌐 Scarica HTML Formattato"** per scaricare direttamente la pagina `.html` o stamparla/salvarla in PDF.

7. **Validazione Euristica dei Dati**:
   - Controllo ed allineamento automatico del Codice Fiscale cliente (con gestione dell'omocodia), della P.IVA del fornitore e della coerenza con Cognome e Nome.

---

## 🛠️ Dettaglio delle Funzioni e dei Moduli Python

### 1. Validazione Dati & Gestione Omocodia
- `_cf_code(word, is_name)`: Calcola i 3 caratteri del codice fiscale per cognome o nome.
- `_find_word_for_code(ocr_text, code, is_name)`: Cerca nel testo estratto una parola alfabetica corrispondente al blocco del CF.
- `validate_fields(data, raw_text)`: Esegue la validazione euristica post-estrazione del Codice Fiscale, della P.IVA e riallinea Cognome e Nome cliente.

### 2. Estrazione Testo PDF & Chiamate Ollama
- `get_ollama_models()`: Recupera la lista dei modelli presenti su Ollama (`http://127.0.0.1:11434`).
- `extract_pdf_text_native(pdf_path)`: Estrazione veloce del testo nativo con PyMuPDF.
- `extract_pdf_text_ocr(pdf_path)`: Rendering ad alta definizione (200 DPI) e OCR con `RapidOCR`.
- `process_extraction(...)`: Funzione principale che coordina la lettura del documento, la composizione dinamica del prompt, l'invio ad Ollama, il salvataggio dei due file JSON su disco (`_ocr.json` e `_estratto.json`) e la restituzione dell'evidenza visiva in UI.

### 3. Generazione Report HTML & Router HTTP
- `build_html_formatted(data_dict)`: Costruisce la pagina HTML responsive con tabella e dati estratti.
- `ExtractorHTTPRequestHandler`: Handler nativo Python che gestisce le route `GET /`, `GET /api/models`, `POST /api/extract` e `POST /api/export-html`.

---

## 💻 Istruzioni d'Uso & Guida all'Avvio

### 1. Avvio dell'Applicazione
Apri un terminale (PowerShell / Command Prompt) ed esegui il comando:

```bash
python C:\Progetto_AI\Estrae_testo_da_Pdf_richiama_Ollama\estrattore_pdf_webapp.py
```

### 2. Accesso all'Interfaccia Web
L'applicazione aprirà automaticamente il tuo browser al seguente indirizzo locale:
👉 **`http://127.0.0.1:8020/`**

---

## 🧪 Risultati del Test di Validazione End-to-End

È stato condotto un test di verifica sul documento di prova **`CCF_000094.pdf`** utilizzando il modello **`nemotron-3-nano:4b`**. L'estrazione salva automaticamente entrambi i file su disco:

1. **JSON OCR Grezzo (`CCF_000094_ocr.json`)**:
   `C:\Progetto_AI\Estrae_testo_da_Pdf_richiama_Ollama\CCF_000094_ocr.json`

2. **JSON Dati Estratti (`CCF_000094_estratto.json`)**:
   `C:\Progetto_AI\Estrae_testo_da_Pdf_richiama_Ollama\CCF_000094_estratto.json`
