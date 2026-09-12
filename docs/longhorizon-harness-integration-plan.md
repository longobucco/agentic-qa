# Piano di integrazione: LongHorizon-Harness + Claude Sonnet 5

**Stato:** proposta tecnica  
**Data:** 2026-09-08  
**Ambito iniziale:** OSWorld Verified  
**Obiettivo:** confrontare, a modello e ambiente fissati, l'harness Claude Code attuale con il loop Manage–Execute–Audit (MEA) di LongHorizon-Harness.

**Branch proposto:** `exp/osworld-longhorizon-sonnet5`  
**Sistema proposto:** `longhorizon_sonnet5`  
**Baseline:** `agent_computer_sonnet5`

## 1. Decisione proposta

Integrare LongHorizon-Harness come **nuovo runner OSWorld**, senza sostituire né modificare il runner baseline. La prima versione deve trattare `lh-harness` come processo esterno e riusare:

- lo stesso `claude` CLI e lo stesso modello `claude-sonnet-5`;
- lo stesso sandbox Daytona e la stessa immagine OSWorld;
- lo stesso MCP `benchmarks.osworld.mcp.server`;
- gli stessi task, setup e valutatori ufficiali;
- lo stesso formato finale `result.json` / `eval.json` usato da `core`.

Il confronto primario diventa:

| Braccio | Modello | Harness esterno | Desktop tools | Risultati |
|---|---|---|---|---|
| Baseline | Sonnet 5 | Claude Code `-p`, sessione singola | OSWorld MCP | `agent_computer_sonnet5` |
| Trattamento | Sonnet 5 | LongHorizon MEA, più sessioni fresche | stesso OSWorld MCP | `longhorizon_sonnet5` |

Questa è la separazione sperimentale più pulita: cambia l'orchestrazione, non il modello, l'ambiente, l'action space o il giudice.

## 2. Evidenza e limiti delle affermazioni

Il paper descrive LongHorizon-Harness come gestione esplicita dello stato fuori dal contesto, aggiornata solo mediante fatti verificati nell'ambiente. Il ciclo è:

1. **Manager:** legge goal, stato verificato e fallimenti; emette un subtask delimitato.
2. **Executor:** parte con contesto fresco ed è il solo ruolo autorizzato a mutare l'ambiente.
3. **Auditor:** ispeziona in sola lettura e accetta o rifiuta l'avanzamento.
4. **Commit dello stato:** solo l'evidenza accettata entra nello stato durevole; altrimenti il Manager pianifica un recovery.

Risultati pubblicati da riportare senza estrapolazioni:

- Qwen 3.7 Plus: 51,8% → 80,7% su WeaveBench (**+28,9 pp**);
- Qwen 3.7 Plus: 69,7% → 77,2% su Terminal-Bench 2.1 (**+7,5 pp**);
- Qwen 3.7 Plus: 2,8% → 8,3% su OSWorld 2.0 (**+5,5 pp**);
- Claude Opus 4.7: 20,0% → 34,3% su un subset OSWorld 2.0 (**+14,3 pp**).

Non esiste ancora evidenza pubblicata che Sonnet 5 ottenga lo stesso uplift. Per questo progetto è un'ipotesi da testare, non un risultato atteso da assumere.

Riferimenti primari:

- Paper: <https://arxiv.org/abs/2608.01964>
- Codice: <https://github.com/AMAP-ML/LongHorizon-Harness>
- Documentazione/progetto: <https://lh-harness.pages.dev/>

Nota di provenienza: il codice è pubblicato sotto l'organizzazione GitHub **AMAP-ML** e gli autori sono affiliati ad Alibaba/AMAP; “Alibaba DreamX” non è il nome canonico del repository. Conviene usare `AMAP-ML/LongHorizon-Harness` nel paper e nei manifest.

## 3. Stato attuale del repository

L'integrazione parte da seam già adatti:

- `core/run.py`: seleziona un `Runner`, prepara un ambiente per task, mantiene la VM viva durante agent e scoring, gestisce repliche e resume;
- `benchmarks/osworld/benchmark.py`: registra oggi il solo runner `agent_computer_<model>` come `self_eval=True`;
- `benchmarks/osworld/runners/agent_computer.py`: crea la configurazione MCP temporanea, invoca `claude -p`, cattura telemetria e transcript, poi esegue il valutatore ufficiale;
- `benchmarks/osworld/env/sandbox.py`: crea e prepara il desktop Daytona per una singola unità sperimentale;
- `benchmarks/osworld/mcp/server.py`: espone screenshot, accessibility tree e azioni GUI verso il controller;
- `core/results.py`: separa traiettoria, risultato, verdetto e failure infrastrutturali;
- `core/reporting.py`: supporta già confronti A/B e controllo della configurazione fissata.

Sono inoltre già presenti timeout dell'agente, timeout del post-run, retry esponenziale del valutatore e classificazione di rate limit / infra flake. Questi non vanno attribuiti a LongHorizon e non devono essere duplicati nel nuovo runner.

## 4. Architettura target

```text
core.run
  └─ osworld_environment(task)
       ├─ crea/resetta Daytona desktop
       └─ restituisce controller_url
            │
            ▼
     longhorizon_sonnet5.run(...)
       ├─ genera task.md e mcp.json
       ├─ avvia lh-harness (dashboard off)
       │    ├─ Manager  ── read-only MCP
       │    ├─ Executor ── full OSWorld MCP
       │    └─ Auditor  ── read-only MCP
       ├─ importa eventi, round e report nel run dir
       ├─ normalizza telemetria in result.json
       └─ chiama lo stesso scorer ufficiale
            │
            ▼
       eval.json + artifact finali
```

### Invariante fondamentale

La VM deve restare la stessa per tutti i round MEA e fino al completamento dello scoring. Un “checkpoint” in questo esperimento significa stato logico verificato e artefatti del run, **non** snapshot/ripristino della VM fra round. Ripristinare una snapshot cambierebbe lo stato osservato dal task e invaliderebbe l'esecuzione.

### Boundary di mutazione

- L'Executor può usare tutti gli strumenti OSWorld autorizzati.
- Manager e Auditor possono usare solo `screenshot` e `a11y_tree`, più lettura degli artifact LongHorizon.
- Manager/Auditor non devono avere `click`, `type`, `key`, `scroll`, `run_python` o shell.
- Il valutatore ufficiale resta esterno al MEA e viene eseguito una sola volta sullo stato finale.

Questo boundary richiede una verifica reale nella spike: passare lo stesso `.mcp.json` a tutti i ruoli potrebbe esporre tool mutanti all'Auditor. Se la versione fissata di LongHorizon non filtra per ruolo i tool MCP di Claude Code, creare **due server MCP/configurazioni**:

- `osworld`: tool completi, solo Executor;
- `osworld_readonly`: screenshot e a11y, Manager/Auditor.

Non considerare il solo prompt “read-only” una garanzia sufficiente.

## 5. Strategia di integrazione in due stadi

### Stadio A — adapter subprocess, raccomandato

Invocare la CLI LongHorizon dal runner Python. Vantaggi:

- minimo accoppiamento con API Python interne ancora giovani;
- conserva il loop upstream esattamente come pubblicato;
- più facile fissare una versione e riprodurre i risultati;
- consente una prima esecuzione end-to-end prima di progettare estensioni.

Il runner costruisce un comando equivalente a:

```bash
lh-harness run \
  --task /absolute/run_dir/longhorizon/task.md \
  --agent claude_code \
  --model claude-sonnet-5 \
  --mcp-config /absolute/run_dir/longhorizon/mcp.json \
  --workspace /absolute/run_dir/longhorizon/workspace \
  --max-rounds 10 \
  --no-dashboard
```

I flag esatti devono essere ricavati da `lh-harness run --help` della versione fissata; il comando sopra è uno schema, non ancora un contratto stabile.

### Stadio B — adapter nativo, solo se necessario

Valutare un `AgentAdapter` custom o l'uso dell'API Python solo se lo stadio A non espone almeno:

- eventi per ruolo e per round;
- exit reason;
- timeout/failure per episodio;
- modello effettivamente servito;
- token, costo e durata per ruolo;
- audit decision ed evidenza.

Non fare fork dell'upstream prima di aver dimostrato che l'adapter subprocess impedisce una misura necessaria.

## 6. File e modifiche previste

### Nuovi file

`benchmarks/osworld/runners/longhorizon.py`

- preflight di binario/versione;
- generazione sicura di task e MCP config;
- costruzione argv senza shell;
- watchdog del process group;
- import degli artifact LongHorizon;
- normalizzazione della telemetria;
- riuso di cattura stato e scoring OSWorld.

`benchmarks/osworld/longhorizon.py`

- piccole funzioni pure per config, parsing e schema artifact;
- nessuna logica Daytona o di scoring.

`benchmarks/osworld/tests/test_longhorizon_runner.py`

- test offline con un falso binario `lh-harness`;
- test timeout/process-group;
- test di mapping degli esiti;
- test che il modello sia sempre esplicito;
- test che i config MCP non contengano segreti persistenti.

`benchmarks/osworld/tests/fixtures/longhorizon/`

- esempi minimi versionati di run riuscito, bloccato, timeout, audit rejection e output corrotto.

`scripts/run_longhorizon_sonnet5_pilot.sh`

- driver riproducibile per subset pilota;
- niente `--force` implicito;
- versione e configurazione stampate prima dell'avvio.

### File da modificare

`benchmarks/osworld/benchmark.py`

- registrare `longhorizon_sonnet5` come secondo runner `self_eval=True`;
- usare lo stesso `osworld_environment` e serializzazione della baseline.

`benchmarks/osworld/config.py`

- aggiungere knob `OSW_LH_*`;
- separare il nome del sistema dal solo model slug;
- validare che Sonnet 5 sia fissato per entrambi i bracci.

`core/run.py`

- includere i knob LongHorizon in `_HARNESS_ENV_KEYS`;
- nessun cambiamento al lifecycle salvo eventuale hook generico di preflight già supportato.

`benchmarks/osworld/README.md`

- installazione, dry-run, pilot, resume e A/B;
- descrizione degli artifact e delle failure class.

`core/reporting.py` o un nuovo `benchmarks/osworld/analysis/longhorizon_ablation.py`

- metriche MEA e confronto matched-task oltre alla pass rate.

## 7. Configurazione da fissare

La dipendenza deve essere pin esatto, preferibilmente versione PyPI + hash oppure commit Git:

```text
lh-harness==<versione-verificata>
upstream_commit=<sha>
```

Variabili proposte:

| Variabile | Default pilota | Significato |
|---|---:|---|
| `OSW_MODEL` | `claude-sonnet-5` | stesso modello per entrambi i bracci |
| `OSW_LH_MAX_ROUNDS` | `10` | cap iniziale MEA |
| `OSW_LH_MANAGER_TIMEOUT` | `300` | secondi per episodio Manager |
| `OSW_LH_EXECUTOR_TIMEOUT` | `900` | secondi per episodio Executor |
| `OSW_LH_AUDITOR_TIMEOUT` | `300` | secondi per episodio Auditor |
| `OSW_LH_TOTAL_TIMEOUT` | `3600` | wall clock totale per task |
| `OSW_LH_DASHBOARD` | `0` | disabilitato nei benchmark |
| `OSW_LH_VERSION` | obbligatorio | versione attesa dal preflight |
| `OSW_LH_ROLES_MODEL` | vuoto | se valorizzato deve coincidere con `OSW_MODEL` nel confronto primario |

Per il confronto primario usare Sonnet 5 per tutti i ruoli. Modelli differenti per Manager/Auditor sono un esperimento successivo perché introducono un secondo trattamento.

`harness.json` deve registrare almeno:

- versione e commit LongHorizon;
- versione Claude CLI;
- model ID globale e per ruolo;
- max round e timeout per ruolo/totale;
- observation/action space;
- elenco/hash dei tool offerti a ciascun ruolo;
- hash di prompt template e config;
- image digest, release ed evaluator commit già presenti;
- stato delle arm `SELF_VERIFY`, `ENFORCE_SANDBOX`, `RESTRICT_RUN_PYTHON`.

## 8. Contratto del runner e artifact

Ogni unità deve continuare a produrre gli artifact standard:

```text
results/longhorizon_sonnet5/<task-id>/run_<n>/
├── result.json
├── eval.json
├── infra_error.json          # solo se non si arriva a un verdetto valido
├── final.png
├── agent_output.txt          # report finale normalizzato
└── longhorizon/
    ├── task.md
    ├── config.resolved.toml
    ├── manifest.json
    ├── events.jsonl
    ├── report.json
    ├── state/
    ├── rounds/
    └── transcripts/
```

Il runner deve copiare gli artifact dal run root LongHorizon prima che directory temporanee vengano rimosse. Non deve dipendere da ricerca globale sotto `~/.claude` quando LongHorizon offre transcript propri; la ricerca per session ID resta un fallback esplicitamente segnalato.

Campi aggiuntivi in `result.json`:

```json
{
  "harness": "longhorizon",
  "harness_version": "...",
  "harness_commit": "...",
  "mea_rounds_started": 0,
  "mea_rounds_verified": 0,
  "mea_rounds_rejected": 0,
  "mea_recoveries": 0,
  "mea_exit_reason": "done|blocked|max_rounds|timeout|error",
  "role_timeouts": {},
  "role_models_served": {},
  "role_usage": {},
  "audit_integrity_violations": 0,
  "transcript_saved": true
}
```

La somma di token/costo/durata va conservata sia aggregata sia per ruolo. Se l'upstream non espone un dato, salvarlo come `null`, mai stimarlo silenciosamente.

## 9. Semantica di completion, retry e resume

Non confondere tre livelli:

1. **Audit MEA:** decide se una milestone entra nello stato verificato.
2. **Exit LongHorizon:** decide se il loop pensa di avere finito, è bloccato o ha esaurito il budget.
3. **OSWorld evaluator:** unico arbitro della metrica benchmark.

Regole:

- `MEA done` + evaluator success → `SUCCESS` pulito;
- `MEA done` + evaluator failure → `FAILURE`, segnale di false completion;
- `max_rounds`/timeout + evaluator success → success incidentale, con annotazione analoga alla baseline;
- `blocked` con desktop valido → eseguire comunque il valutatore e registrare il reward;
- rate limit prima di qualunque azione → `RATE_LIMITED`, nessun `eval.json`, resume automatico;
- crash del processo o artifact illeggibili → `HARNESS_ERROR`, nessun `eval.json`;
- proxy/controller transitorio → `INFRA_FLAKE`, nessun `eval.json`;
- audit rejection → non è infra failure: è una transizione normale del trattamento.

Il resume fra invocazioni di `core.run` inizialmente resta **a livello di task/run**: un'unità senza `eval.json` riparte su una VM fresca. Il resume dello stesso run LongHorizon dopo crash è una capability separata da introdurre solo se:

- l'upstream garantisce ripresa deterministica degli artifact;
- la stessa VM Daytona è ancora viva e identificabile;
- il tempo passato fra tentativi non rende lo stato non confrontabile.

Senza queste tre condizioni, riprendere solo lo stato logico su una VM nuova sarebbe scorretto.

## 10. Milestone di implementazione

### M0 — freeze e spike di compatibilità

Deliverable:

- versione/commit fissati;
- output di `lh-harness --version`, `run --help` e `doctor` archiviato;
- una chiamata Manager, Executor e Auditor su falso MCP locale;
- matrice osservata di tool disponibili per ruolo;
- schema reale degli artifact documentato.

Gate: nessun ruolo read-only può chiamare un tool mutante. Se fallisce, implementare lo split MCP prima di proseguire.

### M1 — runner offline con fake CLI

Deliverable:

- runner e config builder;
- parser degli artifact;
- mapping completo degli exit status;
- test unitari senza rete né Daytona;
- dry-run che mostra argv redatto e paths.

Gate: tutti i test esistenti e nuovi passano; nessuna modifica ai risultati baseline.

### M2 — smoke test Daytona

Eseguire 2–3 task brevi, uno per classi di applicazione differenti. Verificare:

- stessa VM lungo tutti i round;
- azioni solo dall'Executor;
- Auditor davvero read-only;
- evaluator ufficiale eseguito sullo stato finale;
- cleanup di processi MCP/Claude/LongHorizon;
- telemetria e transcript completi.

Gate: zero processi orfani, zero leak di temp file, risultati riproducibili dal manifest.

### M3 — pilot matched-pairs

Selezionare 20–30 task prima di osservare il trattamento:

- stratificati per app e difficoltà proxy;
- includere task con baseline long-running, loop, false completion e max-turn;
- escludere environment error noti secondo le regole già in uso;
- stessa lista esatta per entrambi i bracci;
- almeno 3 run per task.

Gate suggerito per passare alla campagna:

- nessuna regressione di sandbox/evaluator integrity;
- tasso `HARNESS_ERROR + INFRA_FLAKE` non peggiore di 5 pp;
- almeno un segnale positivo tra pass rate matched, riduzione false completion o riduzione stalli;
- costo e latenza osservati entro il budget approvato.

### M4 — ablation dei meccanismi

Solo dopo aver validato l'integrazione full MEA:

1. baseline Claude Code;
2. MEA completo;
3. MEA senza Auditor indipendente;
4. MEA senza stato verificato persistente;
5. MEA con un solo round, per misurare overhead puro;
6. opzionale: ruoli economici vs tutti Sonnet 5.

Le ablation devono usare supporto ufficiale upstream o una patch minimale versionata. Non simulare “Auditor off” con un prompt diverso se cambia anche altro.

### M5 — campagna e analisi finale

- freeze definitivo di codice, dipendenze e manifest task;
- randomizzare/interlacciarе l'ordine dei bracci per ridurre effetti temporali di API e infrastruttura;
- 3 run/task o più, in base alla varianza del pilot;
- report matched-task con intervalli di confidenza;
- pubblicare configurazioni, taxonomy failure e artifact non sensibili.

## 11. Piano sperimentale e metriche

### Endpoint primario

Differenza di pass rate OSWorld fra `longhorizon_sonnet5` e `agent_computer_sonnet5`, sugli stessi task e con stessa configurazione valutatore.

Usare un confronto paired a livello task. Riportare:

- delta assoluto in punti percentuali;
- intervallo di confidenza bootstrap paired;
- conteggio discordante baseline-only vs LongHorizon-only;
- sensibilità all'aggregazione per-run e majority-per-task.

### Metriche secondarie

- clean-success rate;
- false-completion rate: harness dichiara done ma evaluator fallisce;
- timeout/max-round/blocked rate;
- task con almeno un audit rejection poi recuperati;
- numero round totali, accettati e rifiutati;
- tool call e tool call per progresso verificato;
- durata wall-clock e per ruolo;
- token e costo totali, per ruolo e per successo;
- infra/error rate;
- audit integrity violations;
- loop/stall rate secondo una definizione automatica pre-registrata.

### Definizione di loop/stallo

Non usare una valutazione post-hoc vaga. Prima del pilot congelare una regola, per esempio:

- stessa sequenza normalizzata di tool/target ripetuta almeno `k` volte senza nuovo audit fact;
- oppure nessuna nuova milestone verificata per `n` round consecutivi;
- oppure episodio terminato per timeout con hash/screenshot finale sostanzialmente invariato.

Calibrare `k` e `n` su transcript baseline, poi non modificarli dopo aver visto gli outcome del trattamento.

## 12. Principali rischi e mitigazioni

| Rischio | Conseguenza | Mitigazione |
|---|---|---|
| Auditor può mutare GUI via MCP | verifica non indipendente | split MCP + test canary di mutazione |
| Tre ruoli moltiplicano costo/session limit | confronto incompleto o rate limit | pilot piccolo, contatori per ruolo, interleaving bracci |
| Timeout baseline e MEA non equivalenti | budget confounded | confrontare anche sotto cap wall-clock/costo comune |
| CLI upstream cambia rapidamente | irriproducibilità | pin esatto, commit e schema artifact nel manifest |
| LongHorizon usa workspace locale mentre GUI è remota | checkpoint file non coincide con VM | stato harness locale, stato task verificato via controller; niente falso restore |
| Manager/Auditor usano shell o tool estranei | sandbox escape | strict MCP e deny list equivalente alla baseline |
| Evaluator influenzato dall'Auditor | leakage o mutazione | Auditor read-only; official evaluator solo dopo il loop |
| Output `done` non coincide con successo reale | risultato gonfiato | official evaluator resta source of truth |
| Più contesto totale del baseline | uplift attribuito solo al budget | riportare cost-normalized e wall-clock-matched analysis |
| Processi figli sopravvivono al timeout | VM/run contaminati | process group kill + test regressione |

## 13. Criteri di accettazione dell'integrazione

L'integrazione è tecnicamente pronta quando:

- il nuovo sistema è selezionabile da `benchmarks.osworld.run --system longhorizon_sonnet5`;
- il baseline non cambia byte-for-byte nei path di esecuzione non LongHorizon;
- modello, CLI, harness, image, evaluator, prompt e toolset sono tracciabili per run;
- Manager/Auditor sono dimostrabilmente read-only;
- ogni round ha transcript, timestamps, exit reason e audit result;
- rate limit, timeout, crash e infra flake non diventano failure del modello;
- l'evaluator ufficiale produce il verdetto finale mentre la VM è ancora viva;
- resume e `--runs N` mantengono la semantica corrente;
- test offline e smoke Daytona passano;
- il report A/B rifiuta o segnala configurazioni non comparabili.

La ricerca può dichiarare un beneficio dell'harness solo se l'effetto supera la varianza osservata e resta visibile almeno nell'analisi paired primaria. In caso contrario, il risultato utile sarà la caratterizzazione di quali failure mode MEA corregge, introduce o semplicemente sposta.

## 14. Ordine operativo immediato

1. Fissare release/commit di LongHorizon e archiviarne help/config schema.
2. Eseguire M0 con un MCP canary che registra chiamate per ruolo.
3. Decidere, sulla base del canary, se servono due MCP distinti.
4. Implementare runner subprocess e test fixture.
5. Aggiungere il sistema senza toccare il runner baseline.
6. Eseguire dry-run, test offline e tre smoke Daytona.
7. Congelare manifest del pilot e definizione di stall/loop.
8. Eseguire pilot paired, 3 run/task.
9. Fare power/cost review e decidere la scala della campagna.
10. Solo dopo, implementare le ablation MEA.

## 15. Isolamento del branch e dipendenze

Il branch LongHorizon deve partire dallo stesso commit base del branch Verify–Replan, ma non deve dipendere dal codice sperimentale di quel branch. È ammesso condividere soltanto un refactor preliminare delle primitive OSWorld, preferibilmente tramite un piccolo branch/commit neutro applicato a entrambi.

Sequenza raccomandata:

1. creare un nuovo git worktree per evitare le modifiche e gli artifact della campagna attiva;
2. applicare il commit neutro che estrae da `agent_computer.py` telemetria, provenance, capture e scoring senza modificare la baseline;
3. fissare `lh-harness` con versione e commit upstream;
4. aggiungere la dipendenza in modo riproducibile, senza installazioni globali richieste dal runner;
5. conservare l'ambiente virtuale e i risultati del branch separati dalla baseline storica;
6. non versionare `.lh-harness/runs`, dashboard state, token, controller URL o transcript non redatti.

Se il progetto non adotta un file di dipendenze generale, aggiungere un file dedicato come `requirements-longhorizon.txt` con hash oppure uno script di setup che verifichi una versione già installata. Non modificare implicitamente `.venv` come parte dell'implementazione committata.

## 16. Sequenza di commit sul branch

1. `test(osworld): characterize longhorizon integration seams`
2. `build(osworld): pin longhorizon harness dependency`
3. `feat(osworld): add role-safe MCP configurations`
4. `feat(osworld): add longhorizon subprocess adapter`
5. `feat(osworld): normalize MEA artifacts and telemetry`
6. `feat(osworld): register sonnet5 longhorizon runner`
7. `test(osworld): cover MEA exits timeouts and cleanup`
8. `docs(osworld): document longhorizon experiment`
9. `chore(osworld): add pinned pilot manifest and driver`

Tenere separati refactor, dipendenza, behavior e pilot data rende possibile revisionare e, se necessario, revertire ogni livello senza toccare la baseline.

## 17. Test aggiuntivi obbligatori prima del pilot

Oltre ai test descritti nelle milestone:

- **fake LongHorizon CLI:** simulare `done`, `blocked`, `max_rounds`, timeout di ciascun ruolo, rate limit, report mancante e JSON corrotto;
- **process tree cleanup:** dimostrare che timeout del wrapper termina anche Claude e MCP discendenti;
- **role capability test:** enumerare e archiviare i tool realmente visibili a Manager, Executor e Auditor;
- **audit mutation canary:** far tentare esplicitamente all'Auditor una mutazione e verificare che il protocollo la rifiuti;
- **same-VM test:** scrivere un canary con Executor e osservarlo con Auditor senza ricreare Daytona;
- **official-eval ordering:** verificare che lo scorer venga chiamato solo dopo l'ultimo round e prima del teardown;
- **model pin test:** fallire o marcare non comparabile se uno dei ruoli non è servito da Sonnet 5;
- **artifact completeness:** ogni round deve avere ruolo, session ID, modello, timestamps, exit reason e audit decision;
- **baseline non-regression:** nessun import o dependency LongHorizon nel percorso `agent_computer_sonnet5`.

## 18. Definition of Done per lo sviluppo

- `longhorizon_sonnet5` è selezionabile tramite il runner OSWorld standard.
- La versione upstream è fissata e verificata dal preflight.
- La stessa VM sopravvive a tutti i round e allo scoring.
- Solo Executor dispone di tool mutanti.
- Manager/Auditor non ricevono evaluator spec, gold o reward.
- Gli artifact MEA sono copiati nel run dir e descritti da schema versionato.
- Costi, token, durata e modello sono disponibili per ruolo o esplicitamente `null`.
- Exit LongHorizon e verdetto OSWorld rimangono concetti distinti.
- Rate limit/infra error non vengono contati come fallimenti del modello.
- Test offline, fake CLI, MCP integrity e smoke Daytona passano.
- Il pilot usa un manifest congelato e task identici alla baseline.
- Il report include paired delta e analisi natural-budget/matched-budget.

## 19. Relazione con Verify–Replan minimale

I due branch rispondono a domande differenti:

- Verify–Replan: è sufficiente verificare la completion e concedere una correzione mirata?
- LongHorizon: una gestione MEA continua dello stato produce un beneficio ulteriore?

Il confronto finale ideale ha tre bracci: baseline, Verify–Replan e LongHorizon. Evitare però di eseguire subito un'unica campagna a tre bracci: validare prima indipendentemente integrità e costi di ciascun trattamento, quindi congelare una configurazione comune per il confronto.
