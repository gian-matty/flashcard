# Flashcard AI

Carichi un documento PDF, DOCX o PPTX, l'app ne ricava delle flash card e
tu le studii: clicchi sulla card per vedere la risposta, poi decidi se l'hai
ripassata bene. Ogni giudizio aggiorna il calendario delle revisioni future.

Funziona **senza chiavi API**. Con una chiave Gemini le card sono di qualita'
superiore e il bottone "Spiega" scrive una spiegazione su misura; senza chiave
l'app usa un generatore basato su TF-IDF e il bottone mostra la frase del
documento da cui la card nasce.

## Come funziona

```
documento ──▶ estrazione testo ──▶ generazione card ──▶ filtro ──▶ studio
                (con provenienza)     (AI o locale)        ▲           │
                                                   spiega ◀─────────┘
                                                (AI o fonte)
```

Ogni card ricorda da dove viene. Per una definizione salvata da una slide
sai sempre quale slide rileggere.

## Avvio rapido

Serve Python 3.10 o superiore.

```bash
git clone https://github.com/gian-matty/flashcard.git
cd flashcard

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env

python app.py
```

Apri <http://127.0.0.1:5000>.

## La chiave API è facoltativa

L'app funziona interamente offline senza chiavi. Se vuoi le card generate da
Gemini e le spiegazioni AI, prendi una chiave su
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) e mettila in
uno dei due modi:

**Dal browser** (consigliato per un deploy condiviso). Apri *Impostazioni*,
incolla la chiave e salva. Resta nel `localStorage` del tuo browser e non
tocca mai il disco del progetto. Ogni utente del sito usa la propria chiave.

**Dal file `.env`**, per un'istanza personale:

```
GEMINI_API_KEY=AIza...
GEMINI_MODEL=gemini-2.5-flash
```

La chiave non finisce mai nel database né nel git: `.env` è già in
`.gitignore`.

Come viene risolta la chiave, in ordine:

1. header `X-Gemini-Key` della richiesta, cioè quella che hai salvato nel browser
2. variabile d'ambiente `GEMINI_API_KEY` del server
3. nessuna: entra in funzione il generatore locale

Se Gemini fallisce o esaurisce la quota, l'app ricade in automatico sul
generatore locale senza perdere il lavoro.

## Come si studia

| Azione | Effetto |
| --- | --- |
| Click sulla card | mostra la risposta |
| **Giusta** | l'intervallo si allunga, la card torna più avanti |
| **Ancora** | la rivedi presto, senza penalizzarla |
| **Sbagliata** | il fattore di disinvoltura scende, la rivedi a breve |
| **Spiega con l'AI** | giudizio, spiegazione e concetto chiave |
| *Tutte le card* | elenco completo con stato di ogni card |

Le revisioni seguono SM-2, l'algoritmo di SuperMemo: ogni risposta giusta
moltiplica l'intervallo per il fattore di disinvoltura della card, ogni errore
lo riduce e azzera il contatore delle ripetizioni.

## Tipi di card

Con Gemini il modello sceglie. Senza chiave, il generatore locale produce
quattro tipi diversi, per non ripetere sempre lo stesso formato:

- **Cloze** — un termine significativo diventa il buco:
  *Il ciclo di Krebs produce ATP e ______* → NADH
- **Definizione** — dalle frasi che definiscono: *Che cos'è il mitocondrio?*
- **Sezione** — cosa dice una slide o un heading del documento
- **Riassunto** — i punti principali, con punteggio TF-IDF e bonus di posizione

La selezione dei termini usa TF-IDF calcolato in casa: le parole che compaiono
in tutte le pagine hanno peso circa nullo e vengono scartate, restano quelle
davvero distintive del documento.

Le card generate passano da una schermata di revisione: le vedi tutte, filtri
per tipo, scarti quelle che non ti servono. Solo il salvataggio le scrive nel
database, quindi non trovi sorprese dentro il mazzo.

## Formati supportati

| Formato | Cosa viene estratto |
| --- | --- |
| PDF | testo per pagina, con i paragrafi ricollassati |
| DOCX | paragrafi raggruppati per heading, più le tabelle |
| PPTX | una card per slide, con le note del relatore |

Limiti: 25 MB per file. I PDF scansionati non hanno testo da estrarre e non
sono supportati, servirebbe un OCR.

## Deploy

GitHub Pages non può eseguire il backend Flask, quindi serve una piattaforma
con supporto Python. Il `Procfile` è già pronto per Render e Railway.

**Render**

1. Nuovo *Web Service* collegato a `github.com/gian-matty/flashcard`
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn app:app`
4. Variabili d'ambiente: `GEMINI_API_KEY` se vuoi il server con AI attiva,
   `SECRET_KEY` con un valore casuale

**Railway**

`New Project` → *Deploy from GitHub repo*. Railway rileva il `Procfile` e il
Python da `requirements.txt`.

Sia Render che Railway nel piano gratuito hanno il filesystem temporaneo:
database e documenti caricati vengono azzerati a ogni deploy, e le app sono
condivise tra tutti gli utenti che visitano l'URL. Va bene per uso personale.
Per un uso serio servono Postgres e un autenticazione sugli ID dei deck.

## Struttura

```
app.py              rotte Flask, upload, gestione delle chiavi
backend/
  db.py             schema SQLite e CRUD
  extract.py        PDF/DOCX/PPTX, testo e provenienza
  cards.py          generatore locale: TF-IDF, cloze, definizioni, riassunti
  gemini.py         chiamate a Gemini: card e spiegazioni
  srs.py            scheduling spaced repetition (SM-2)
static/             CSS e JavaScript, nessuna dipendenza esterna
templates/index.html
data/               database e documenti caricati, fuori dal git
```

### API

| Metodo | Rotta | Funzione |
| --- | --- | --- |
| `GET` | `/api/health` | stato del servizio e disponibilità AI |
| `POST` | `/api/documents` | upload, validazione, estrazione |
| `GET` | `/api/documents` | documenti caricati |
| `DELETE` | `/api/documents/<id>` | elimina documento, mazzi e file |
| `POST` | `/api/documents/<id>/cards` | genera le card, non le salva |
| `POST` | `/api/documents/<id>/decks` | salva le card scelte in un mazzo |
| `GET` | `/api/decks` | mazzi con le revisioni in scadenza |
| `GET` | `/api/decks/<id>/cards` | card del mazzo, filtro `?type=` |
| `GET` | `/api/decks/<id>/next` | prossima card da ripassare |
| `POST` | `/api/cards/<id>/review` | registra il giudizio e ricalcola SM-2 |
| `POST` | `/api/cards/<id>/explain` | spiegazione AI, oppure la fonte |
| `DELETE` | `/api/cards/<id>` | elimina una card |

### Database

Quattro tabelle in SQLite: `documents`, `decks`, `cards`, `reviews`. Ogni card
conserva `source_text`, `source_page` e `source_kind`, che sono la provenienza
usata dal bottone "Spiega".

## Configurazione

| Variabile | Default | Descrizione |
| --- | --- | --- |
| `GEMINI_API_KEY` | vuota | chiave API del server |
| `GEMINI_MODEL` | `gemini-2.5-flash` | modello usato |
| `SECRET_KEY` | valore di sviluppo | segreto di sessione, da cambiare in produzione |
| `PORT` | `5000` | porta del server |
| `MAX_UPLOAD_MB` | `25` | limite di upload |

## Sicurezza

I file vengono validati per estensione, magic bytes e dimensione, e salvati con
un nome UUID, quindi il nome originale non può influenzare il percorso. Tutti
i valori passati al frontend passano da `esc()` prima di entrare nel DOM.