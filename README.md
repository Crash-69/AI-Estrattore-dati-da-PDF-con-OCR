# AI-Estrattore-dati-da-PDF-con-OCR

Webapp locale per estrarre dati da PDF, scansioni e immagini con RapidOCR e un modello Ollama. L’interfaccia non usa servizi o risorse esterne; i file sono elaborati in memoria e non vengono salvati dall’applicazione.

## Requisiti

- Python 3.10 o superiore
- [Ollama](https://ollama.com/) installato e avviato
- Un modello Ollama, ad esempio `llama3.2`

## Avvio

```bash
python -m pip install -r requirements.txt
ollama pull llama3.2
python estrattore_pdf_webapp.py
```

Apri [http://127.0.0.1:8000](http://127.0.0.1:8000). L’app accetta PDF e immagini fino a 20 MB e PDF fino a 30 pagine; il testo già presente nei PDF viene estratto direttamente, mentre scansioni e immagini vengono elaborate con RapidOCR.

Per usare un endpoint Ollama diverso da `http://127.0.0.1:11434`, imposta la variabile d’ambiente `OLLAMA_URL` prima dell’avvio. I documenti vengono inviati all’endpoint configurato per la strutturazione JSON.

## Test

```bash
python -m unittest discover -s tests -v
```
