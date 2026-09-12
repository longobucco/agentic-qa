# Piano di implementazione: Verify–Replan minimale per OSWorld + Sonnet 5

**Stato:** implementato e pilotato — **chiuso, gate di stop/go non superato** (vedi §18)  
**Data:** 2026-09-08 (piano) — 2026-09-10/11 (implementazione + pilota)  
**Branch:** `verify-replan`  
**Sistema:** `verify_replan_sonnet5` (`verify_replan_sonnet5_auditonly` per il braccio senza recovery)  
**Baseline:** `agent_computer_sonnet5`

## 1. Obiettivo e ipotesi

Implementare il più piccolo harness aggiuntivo capace di correggere il failure mode “l'agente dichiara `DONE`, ma lo stato finale non soddisfa il valutatore”, mantenendo invariati modello, Claude Code CLI, ambiente Daytona, strumenti MCP, task e valutatore ufficiale.

Ipotesi primaria:

> Un audit indipendente e read-only seguito, solo quando necessario, da una nuova sessione Claude Code con evidenza concreta migliora la pass rate di OSWorld Verified rispetto alla sessione singola, a parità di budget totale.

L'esperimento non introduce pianificazione multi-agente continua, memoria generale o checkpoint della VM. Il trattamento è deliberatamente stretto:

```text
Execute → Verify → [Done | Replan → Execute recovery → Verify]
```

## 2. Non-obiettivi

- Non sostituire Claude Code con Agent SDK o Messages API.
- Non cambiare screenshot+a11y, action space o tool implementation.
- Non esporre il valutatore ufficiale o le reference all'agente.
- Non riusare il reward ufficiale come segnale di replan.
- Non introdurre LongHorizon-Harness in questo branch.
- Non modificare il comportamento del runner baseline.
- Non ripristinare snapshot fra execution e recovery.
- Non aumentare implicitamente il budget senza registrarlo.

## 3. Principi sperimentali

### 3.1 Stesso backbone

Executor iniziale, Auditor e Recovery Executor usano tutti `claude-sonnet-5` nel confronto principale. Varianti con auditor economico appartengono a una futura ablation.

### 3.2 Audit non-oracolare

L'Auditor può osservare solo ciò che un utente vedrebbe o potrebbe leggere tramite strumenti pubblici del computer:

- screenshot;
- accessibility tree;
- opzionalmente lettura di artifact esplicitamente prodotti dal task, ma solo tramite un nuovo tool read-only controllato.

Non riceve:

- evaluator spec del task;
- reference/gold file;
- reward o output del valutatore ufficiale;
- metadati che rivelino la soluzione;
- transcript completo dell'Executor, salvo un breve claim finale strutturato.

### 3.3 Audit realmente read-only

Il processo Auditor deve vedere una configurazione MCP che espone esclusivamente tool read-only. Un prompt “non modificare” non è una misura di sicurezza sufficiente.

### 3.4 Budget confrontabile

Devono essere prodotti due risultati distinti:

1. **Natural budget:** baseline con budget corrente; trattamento con execution + audit + recovery.
2. **Matched budget:** stesso limite complessivo di wall-clock e, quando misurabile, token/costo.

Il paper non deve presentare il primo come puro effetto dell'harness senza mostrare il secondo.

## 4. State machine

```text
START
  │
  ▼
EXECUTE_INITIAL
  ├─ API/infra error prima di azioni ──► INFRA_ERROR
  └─ stato desktop disponibile
          │
          ▼
       AUDIT_1
          ├─ VERIFIED_DONE ────────────► SCORE
          ├─ NOT_DONE + evidence ──────► REPLAN
          ├─ INFEASIBLE + evidence ────► SCORE
          └─ AUDIT_ERROR ──────────────► SCORE_WITH_AUDIT_ERROR
                                           oppure INFRA_ERROR,
                                           secondo policy congelata
REPLAN
  │ produce recovery contract
  ▼
EXECUTE_RECOVERY
  ├─ max recovery raggiunto ───────────► FINAL_AUDIT
  └─ stato desktop disponibile ────────► FINAL_AUDIT
                                           │
                                           ▼
                                         SCORE
```

Lo scorer ufficiale viene invocato una sola volta, alla fine, mentre la stessa VM è ancora attiva.

## 5. Contratti strutturati

### 5.1 Executor claim

La risposta dell'Executor continua a terminare con `ANSWER:`, ma il prompt richiede anche un blocco finale parsabile:

```json
{
  "status": "done|infeasible|blocked",
  "claim": "descrizione sintetica dello stato ottenuto",
  "evidence_to_check": ["elemento osservabile 1", "elemento osservabile 2"],
  "remaining_risk": "eventuale dubbio"
}
```

Un blocco mancante non annulla la traiettoria: l'Auditor riceve task originale e claim testuale disponibile.

### 5.2 Audit report

```json
{
  "schema_version": 1,
  "verdict": "verified_done|not_done|infeasible|uncertain|audit_error",
  "observations": [
    {
      "fact": "fatto osservato",
      "source": "screenshot|a11y|artifact",
      "evidence_ref": "audit_1/screenshot_002.png"
    }
  ],
  "failed_checks": ["condizione non soddisfatta"],
  "next_action": "correzione concreta, senza prescrivere coordinate",
  "confidence": "high|medium|low"
}
```

Solo `not_done` con almeno un'osservazione e una correzione concreta autorizza il recovery. `uncertain` non viene automaticamente trasformato in failure dell'Executor.

### 5.3 Recovery contract

La nuova sessione riceve:

- task originale;
- claim dell'Executor precedente;
- audit report completo;
- screenshot/a11y più recenti;
- budget residuo;
- istruzione di correggere esclusivamente i check falliti e poi verificare nuovamente.

Non riceve evaluator spec, gold o reward.

## 6. Architettura nel repository

```text
core.run
  └─ osworld_environment(task)
      └─ verify_replan.run(task, env, out)
          ├─ InitialExecutor (claude -p + full MCP)
          ├─ ReadOnlyAuditor (claude -p + readonly MCP)
          ├─ RecoveryExecutor (opzionale, full MCP)
          ├─ FinalAuditor (readonly MCP)
          ├─ capture final state
          └─ official OSWorld evaluator
```

La logica comune oggi privata in `agent_computer.py` non va copiata integralmente. Estrarre solo primitive stabili e prive di policy in un modulo condiviso.

## 7. Piano dei file

### 7.1 Nuovi file

`benchmarks/osworld/runners/verify_replan.py`

- orchestra le fasi;
- conserva la stessa VM;
- scrive artifact per fase;
- applica timeout e budget residuo;
- chiama una sola volta cattura finale e scorer.

`benchmarks/osworld/verification.py`

- schema e parsing di claim/audit;
- generazione dei prompt;
- decisione pura `should_recover(audit, budget, attempts)`;
- nessuna dipendenza Daytona.

`benchmarks/osworld/mcp/readonly_server.py`

- espone solo `screenshot`, `a11y_tree`, `wait` se `wait` è dimostrato non mutante;
- non importa né registra funzioni di input;
- usa lo stesso controller URL.

`benchmarks/osworld/tests/test_verify_replan.py`

- state machine, parsing, budget, outcome mapping.

`benchmarks/osworld/tests/test_readonly_mcp.py`

- enumerazione tool;
- prova negativa per click/type/key/run_python;
- verifica che screenshot/a11y non mutino uno stato canary.

`benchmarks/osworld/tests/fixtures/verify_replan/`

- claim valido/mancante;
- audit pass/fail/uncertain/corrotto;
- rate limit e timeout per ciascun ruolo.

`scripts/run_verify_replan_sonnet5_pilot.sh`

- driver del pilot con manifest congelato.

### 7.2 Modifiche previste

`benchmarks/osworld/runners/agent_computer.py`

- estrarre helper condivisibili senza cambiare il percorso baseline:
  - creazione MCP config;
  - telemetria;
  - provenance/model mismatch;
  - bounded post-run;
  - capture e score.

Modulo proposto: `benchmarks/osworld/runners/common.py`.

Prima dell'estrazione aggiungere characterization test che congelino output e semantica baseline. L'estrazione deve essere un commit separato dal nuovo harness.

`benchmarks/osworld/benchmark.py`

- aggiungere runner `verify_replan_sonnet5`;
- stesso environment e `self_eval=True`.

`benchmarks/osworld/config.py`

- aggiungere knob `OSW_VR_*`;
- non alterare default baseline.

`core/run.py`

- registrare knob Verify–Replan in `harness.json`.

`benchmarks/osworld/README.md`

- documentare esecuzione, risultati e limiti.

## 8. Configurazione

| Variabile | Default pilot | Note |
|---|---:|---|
| `OSW_MODEL` | `claude-sonnet-5` | obbligatorio nel trattamento |
| `OSW_VR_MAX_RECOVERIES` | `1` | massimo consigliato su Verified |
| `OSW_VR_INITIAL_TIMEOUT` | `2400` | execution iniziale |
| `OSW_VR_AUDIT_TIMEOUT` | `300` | per audit |
| `OSW_VR_RECOVERY_TIMEOUT` | `900` | sessione correttiva |
| `OSW_VR_TOTAL_TIMEOUT` | `3600` | cap hard dell'intero task |
| `OSW_VR_AUDITOR_MODEL` | vuoto | eredita `OSW_MODEL` |
| `OSW_VR_MIN_CONFIDENCE` | `medium` | non recuperare su low-confidence |
| `OSW_VR_AUDIT_ON_FAIL` | `1` | audit anche dopo `FAIL`/infeasible |
| `OSW_VR_CAPTURE_EVERY_OBS` | `1` | evidenza riproducibile |

`OSW_MAX_TURNS` non può essere assegnato integralmente a ogni sessione. Definire cap distinti:

- initial executor: 100 turn;
- auditor: 12 turn;
- recovery executor: 38 turn;
- final auditor: 12 turn;

Il totale dei turn dell'Executor deve essere confrontato anche con la baseline a 150; i turn di audit vanno riportati separatamente e inclusi nei costi.

## 9. Prompt design

### Initial Executor

Riutilizzare `agent_prompt(task)` senza alterazioni sostanziali. Aggiungere soltanto il contratto finale e chiarire che un processo esterno verificherà il risultato.

### Auditor

Prompt stabile e generico:

- controlla il goal usando soltanto lo stato osservabile;
- non fidarti del claim;
- non tentare azioni correttive;
- elenca fatti verificati e lacune;
- se non è possibile verificare, restituisci `uncertain`;
- non dedurre successo dall'apertura dell'app o dall'esistenza di un file senza verificarne il contenuto pertinente.

### Recovery Executor

- considera l'audit evidenza, non verità assoluta;
- re-osserva prima di agire;
- preserva quanto già corretto;
- applica la modifica minima necessaria;
- non ricomincia l'intero task salvo stato inconsistente;
- termina con lo stesso claim strutturato.

Ogni template deve avere versione e SHA-256 registrati.

## 10. Artifact layout

```text
results/verify_replan_sonnet5/<task>/run_<n>/
├── result.json
├── eval.json
├── final.png
├── agent_output.txt
├── conversation.jsonl
└── verify_replan/
    ├── manifest.json
    ├── timeline.jsonl
    ├── initial/
    │   ├── output.txt
    │   ├── conversation.jsonl
    │   └── claim.json
    ├── audit_1/
    │   ├── report.json
    │   ├── conversation.jsonl
    │   └── screenshots/
    ├── recovery_1/
    │   ├── contract.json
    │   ├── output.txt
    │   └── conversation.jsonl
    └── audit_final/
        ├── report.json
        └── conversation.jsonl
```

`conversation.jsonl` al root può essere un indice JSONL delle sessioni, non una concatenazione ambigua. Ogni record deve includere `role`, `attempt`, `session_id`, `path`, timestamp e modello servito.

Campi aggiuntivi di `result.json`:

```json
{
  "harness": "verify_replan",
  "harness_schema": 1,
  "initial_claim": "done",
  "audit_initial": "not_done",
  "recovery_triggered": true,
  "recovery_attempts": 1,
  "audit_final": "verified_done",
  "false_completion_detected": true,
  "false_completion_recovered": true,
  "role_usage": {},
  "total_agent_cost_usd": null,
  "total_agent_duration_ms": null
}
```

## 11. Error taxonomy

| Evento | Classificazione | Scoring |
|---|---|---|
| Initial executor rate-limited prima di agire | `RATE_LIMITED` | no `eval.json` |
| Auditor rate-limited dopo stato valido | `AUDIT_INFRA_ERROR` | policy pre-registrata: score senza recovery |
| Recovery rate-limited | `RECOVERY_INFRA_ERROR` | score stato esistente, annotato |
| MCP/controller irraggiungibile | `INFRA_FLAKE` | no score se stato non affidabile |
| Parser claim fallisce | agent-format issue | audit comunque |
| Parser audit fallisce dopo retry singolo | `AUDIT_HARNESS_ERROR` | score senza recovery |
| Audit dice done, official eval fallisce | false positive audit | `FAILURE` |
| Audit dice not_done, stato già passa | false negative audit | `SUCCESS`, recovery-harm analysis |
| Recovery peggiora stato | agent failure | verdetto finale + regression flag |

Non rilanciare automaticamente l'intero task sulla stessa run index dopo un verdetto ufficiale.

## 12. Test plan

### Unit test offline

- parsing tollerante dei blocchi JSON;
- tutte le transizioni della state machine;
- cap di recovery;
- budget residuo mai negativo;
- model mismatch per ogni ruolo;
- aggregazione token/costo/durata;
- mapping errori senza contaminare pass rate;
- redazione di URL/segreti nei manifest.

### Integration test con fake Claude CLI

Un eseguibile fittizio restituisce envelope diversi in sequenza:

1. executor done → auditor verified;
2. executor done → auditor not_done → recovery → verified;
3. executor fail → auditor infeasible;
4. auditor output corrotto;
5. timeout con processo nipote;
6. rate limit in ogni fase.

### MCP integrity test

- `list_tools` espone solo allowlist read-only;
- tentativi di invocazione mutante falliscono a livello protocollo;
- hash di un artifact canary invariato prima/dopo audit;
- screenshot prima/dopo invariato salvo animazioni/clock, che vanno mascherati nella comparazione.

### Live smoke test

Tre task:

- task semplice già stabile;
- task con false completion storica;
- task con artifact complesso.

Gate: zero mutazioni Auditor, score ufficiale valido, transcript completo, zero processi orfani.

## 13. Piano sperimentale

### Pilot

- 30 task fissati prima del trattamento;
- 10 false-completion storici;
- 10 flaky/near-boundary;
- 10 controlli sempre-pass/sempre-fail;
- 3 run per task e per braccio;
- ordine interlacciato baseline/trattamento;
- stessa configurazione image/evaluator/model.

Il subset storico serve allo sviluppo, non può essere presentato come stima non distorta dell'intero benchmark. L'endpoint generale va poi misurato su un campione casuale stratificato o sul benchmark completo.

### Bracci

1. baseline sessione singola;
2. audit-only: nessun recovery, misura qualità del verificatore;
3. verify-replan con massimo una recovery;
4. opzionale matched-budget baseline, ridistribuendo lo stesso wall-clock/turn cap.

### Endpoint

Primario: delta paired di pass rate ufficiale.

Secondari:

- false completion rilevate;
- precision/recall dell'Auditor rispetto al verdetto ufficiale, con caveat che osservano segnali diversi;
- recovery success rate;
- recovery harm rate;
- costo e tempo per successo aggiuntivo;
- rate limit e infra error per ruolo;
- risultati per app e classe di task.

### Stop/go dopo pilot

Procedere alla scala se:

- Auditor precision su `verified_done` ≥ 90%;
- recovery harm ≤ 5% dei task sottoposti a recovery;
- almeno 3 successi netti aggiuntivi e nessun segnale di leakage nel pilot;
- overhead mediano entro 2× baseline, oppure costo per successo giudicato accettabile;
- nessuna differenza sistematica di infra validity fra bracci.

## 14. Rischi specifici

### Auditor troppo debole

Screenshot/a11y possono non mostrare proprietà valutate nei file. Mitigazione: aggiungere tool read-only specifici per artifact, generici e non derivati dalle reference del task.

### Auditor troppo informato

Usare evaluator config o gold crea leakage. Mitigazione: boundary dati testato e manifest dei campi forniti a ciascun ruolo.

### Recovery distruttiva

Una correzione può guastare parti già corrette. Mitigazione: contract minimale, re-observe iniziale, final audit e metrica `recovery_harm`.

### Più compute mascherato da harness

Mitigazione: natural-budget + matched-budget; cost/latency reporting obbligatorio.

### Claude Code session transcript incompleto

Mitigazione: acquisire ogni session ID subito, copiare transcript prima di avviare la fase seguente, registrare coverage esplicita.

## 15. Sequenza di commit sul branch

1. `test(osworld): characterize shared runner primitives`
2. `refactor(osworld): extract runner common helpers`
3. `feat(osworld): add read-only audit MCP server`
4. `feat(osworld): add verify-replan state machine`
5. `feat(osworld): register sonnet5 verify-replan runner`
6. `test(osworld): cover role failures and process cleanup`
7. `docs(osworld): document verify-replan experiment`
8. `chore(osworld): add pinned pilot manifest and driver`

Ogni commit deve lasciare verdi i test offline. Non includere artifact della campagna o modifiche già presenti nel worktree.

## 16. Definition of Done

- Baseline invariata e verificata da characterization test.
- Nuovo runner selezionabile esplicitamente.
- Tutti i ruoli serviti da Sonnet 5 o run marcata non comparabile.
- Auditor impossibilitato tecnicamente a mutare il desktop.
- Nessun gold/evaluator leakage.
- Una sola valutazione ufficiale sullo stato finale.
- Telemetria completa per ruolo e aggregata.
- Error taxonomy coerente con `core.reporting`.
- Resume e `--runs N` conservano la semantica corrente.
- Fake-CLI integration suite e live smoke test passano.
- Pilot manifest congelato prima di osservare i risultati.
- Report produce confronto paired, natural-budget e matched-budget.

## 17. Primo task di sviluppo

Creare il branch solo da un worktree pulito o da un nuovo git worktree, poi implementare esclusivamente i primi due commit: characterization test ed estrazione helper. Prima di aggiungere il nuovo runner, dimostrare che una run baseline dry-run produce lo stesso argv e che i test esistenti continuano a passare.

## 18. Risultati (2026-09-10/11) — chiuso, gate non superato

Implementazione: tutti gli 8 commit della sequenza (§15) sul branch `verify-replan`,
in ordine, ciascuno con test verdi prima di procedere al successivo. Tre bug reali
trovati e corretti offline, prima di qualsiasi run dal vivo:
- un timeout di watchdog patchato sul modulo sbagliato durante l'estrazione in
  `runners/common.py` (i lookup delle globali di una funzione risolvono nel modulo
  dove è *definita*, non in quello che la importa);
- un parser JSON che restituiva l'oggetto annidato più interno invece di quello di
  primo livello per un referto con sotto-oggetti (`observations`);
- `config.VR_AUDIT_ON_FAIL` definito ma mai letto dal runner (knob morto).

**Live smoke test** (§12, 10 settembre, 3 task: uno stabile, uno con falso
completamento storico, uno con artefatto complesso): gate a 4 criteri tutti
superati — zero mutazioni dell'Auditor (solo tool read-only invocati, verificato
per trascrizione), punteggio ufficiale valido su tutti e 3, trascrizioni complete,
zero processi orfani.

**Primo lotto pilota** (20 task del manifest congelato da 30, 60 run, 10 settembre,
$27.79): tasso di successo 33,3% (20/60). Su 60 audit, l'Auditor non ha *mai*
detto `not_done` — solo `verified_done` (34), `uncertain` (23), `infeasible` (3).
Zero recovery innescati. Di 34 `verified_done`, 22 corrispondevano a un `FAILURE`
ufficiale: **precisione 35,3%**, contro la soglia di stop/go richiesta per
procedere alla scala (≥90%, §13). Gate fallito nettamente, non al margine.

Lettura delle trascrizioni dei 22 falsi positivi (3 casi dettagliati:
`0326d92d` grafico, `04578141` colori PPTX, `08c73485` filtro Thunderbird):
l'Auditor non era cieco nel senso strutturale dell'idea #15 (G5) — notava spesso
un problema reale nel proprio ragionamento (grafico a barre orizzontali invece di
colonne; "l'albero di accessibilità è vuoto, non posso verificare i valori hex";
un'impostazione che copre solo il caso globale) e poi arrotondava comunque il
verdetto a `verified_done` con confidenza alta. Un problema di calibrazione del
prompt, non del canale di osservazione.

**Correzione applicata** (v2 di `AUDITOR_SYSTEM_PROMPT`, commit `7305485`): ogni
scostamento letterale notato deve finire in `failed_checks` con verdetto
`not_done`, mai derubricato; ogni dettaglio non confermato direttamente abbassa la
confidenza. Rivalidata sui 6 task falsi positivi del primo lotto (18 run, stesso
giorno, $13.72): **precisione salita a 57,1%** (8/14), con un recovery
genuinamente riuscito (`0326d92d` run 2: `not_done` → correzione → `verified_done`
→ SUCCESS). Ancora sotto soglia. Tre task (`04578141`, `08c73485`, `10a730d5`)
restano sbagliati 3/3 identicamente in entrambi i round: colore hex esatto in un
file PPTX, valore di preferenza per-account, override CSS di un tema scuro —
criteri non verificabili da screenshot + albero di accessibilità in linea di
principio, non per una formulazione di prompt migliorabile.

**Conclusione**: il gate di stop/go di questo stesso piano non è superato.
Aggiungere ruoli, contratti strutturati e un turno di recovery non compensa un
canale di osservazione insufficiente per criteri di precisione fine — la stessa
lezione dell'idea #15 (G5), qui confermata su un'architettura molto più elaborata
e a un costo quasi tre volte superiore ($41.51 contro $16.91 di #15). La campagna
pilota completa (30 task, 4 bracci) e ogni scala oltre **non sono state lanciate**.

Direzioni non escluse da questo esito ma fuori scope qui (vedi anche §14): dare
all'Auditor un accesso di sola lettura a contenuti precisi (valori di cella,
XML di uno stile) senza riaprire il rischio di contaminazione dell'oracolo
documentato per l'agente stesso; o un confronto controllato più ampio
(stesso task, molti run, con/senza il meccanismo) per separare un possibile
piccolo effetto reale dal rumore di fondo (~13–23% di flakiness già documentato
altrove in questo progetto) — non tentato qui per ragioni di costo.

