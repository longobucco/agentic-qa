# Piano di implementazione: harness di grounding (Mixture-of-Grounding) per OSWorld + Sonnet 5

**Stato:** implementato e testato — **non lanciato: la premessa non regge su Sonnet 5 pinnato** (vedi §5)
**Data:** 2026-09-11
**Branch:** `grounding-harness`
**Sistema:** `agent_computer_sonnet5_grounding` (suffisso automatico, `config.SYSTEM_NAME`)
**Baseline:** `agent_computer_sonnet5`
**Flag:** `OSW_GROUNDING=1` (off per default; `OSW_GROUNDING_VERIFY`, `OSW_GROUNDING_MIN_SCORE`)

## 1. Perché questo arm e non un altro

Tutti gli arm della serie G5 agiscono **dopo** l'azione: self-verify (#1, nullo), verifier
indipendente (#9), majority vote (#12), verifica in-loop (#15, nullo), Verify-Replan (gate
fallito al 57.1% di precisione), selezione bBoN offline (#16, sotto il caso). Il tetto
strutturale di qualunque meccanismo che agisca solo sull'affidabilità è
**always-pass + flaky**: sul tree pinnato è **62.5%**, e cinque arm non sono riusciti ad
avvicinarlo. Il 37.5% di task che non passano mai è raggiungibile solo cambiando ciò che
l'agente *riesce a fare*.

Questo arm sposta la **risoluzione del target prima** che lo stato sbagliato esista: l'agente
nomina ciò su cui vuole agire (`"il pulsante Grassetto"`, `"cella D7"`) e l'harness risolve il
nome in coordinate attraverso l'albero di accessibilità, con fallback alla stima visiva quando
nulla risolve. È la forma da cui provengono i guadagni pubblicati su OSWorld (Agent S2,
arXiv:2504.00906; UGround/SeeAct-V, arXiv:2410.05243), ed è l'idea #2 del backlog nella sua
versione corretta del 2026-08-28 — mai ripianificata fino a qui.

## 2. Cosa è stato escluso prima di scegliere (tree pinnato, 833 run valutati, 851 transcript)

| Leva candidata | Dato che la esclude |
|---|---|
| Alzare il budget di turni | mediana **17 turni in entrambi i bucket**, **0.0%** dei run al cap di 150 |
| Tool di read-back degli artefatti | l'agente già riapre e parsa i propri file nel **73.2%** dei run always-fail che usano `run_python` |
| Forzare il "diff loop" (calcola atteso → agisci → confronta) | 10.0% negli always-fail vs **12.9%** negli always-pass: nessun arricchimento |
| Decomposizione esplicita del requisito | 1.16 vs 1.11 check congiuntivi, 3.86 vs 3.77 clausole: indistinguibili |
| Ancora verifica/audit | 5 arm, nulli o negativi, e comunque sotto il tetto 62.5% |
| Selezione su N rollout | bBoN 59.4% vs 66.7% del caso puro |
| Togliere `run_python` | strettamente peggiore (#11) |
| Eliminare `ToolSearch` | 1.05 chiamate/run su una mediana di 17: il budget non è vincolante, effetto atteso ≈ 0 |

Restava una sola osservazione non spiegata: `a11y_tree` è usato **137 volte (0.8%)** contro i
4682 screenshot (27.3%). Il canale strutturato che potrebbe risolvere un target per ruolo e nome
è inutilizzato, perché un dump grezzo dell'albero è illeggibile e non offre query.

## 3. Architettura implementata

Tre tool, nessun codice per-applicazione:

| Tool | Cosa fa |
|---|---|
| `find_element(description, role="")` | risolve un elemento **senza** cliccare; restituisce i candidati ordinati con centro, ruolo, nome e **perché** hanno quel rank |
| `click_element(description, role="", rank=1, double, button)` | risolve, clicca il centro, e **riporta se il click non ha cambiato nulla** |
| `list_elements(role="", name_contains="")` | enumerazione compatta degli elementi interattivi a schermo |

- `benchmarks/osworld/grounding.py` — logica pura: parsing dell'albero, scoring, ranking,
  `focused()`, `tree_digest()`. 24 test, nessuna rete.
- `benchmarks/osworld/mcp/grounding_tools.py` — funzioni che prendono un `Controller` e
  restituiscono stringhe, più `register(mcp, ctrl)`. 23 test.
- `benchmarks/osworld/analysis/g10_grounding_signal.py` — metrica di targeting e gate. 10 test.

Scelte di progetto non negoziabili, ciascuna con la sua ragione:

1. **`click(x, y)` resta intatto e disponibile.** È un canale **aggiunto**. La letteratura 2026
   è esplicita sul fatto che gli alberi a11y sono incompleti sui widget a rendering custom
   (canvas di GIMP, visualizzatori d'immagini); forzare ogni click nell'albero scambierebbe un
   failure mode con un altro.
2. **Se nulla risolve, `click_element` non clicca niente.** Non può vedere lo screenshot, quindi
   una coordinata indovinata sarebbe strettamente peggiore della stima dell'agente. Risponde
   dicendo di usare `screenshot()` + `click(x, y)`.
3. **Niente tool per-applicazione, e nessuno previsto.** Una cella di Calc è una `table-cell` il
   cui nome accessibile è il riferimento; un controllo di toolbar è un `push-button` con un nome.
   Stesso percorso generico. Il round precedente di tool di ispezione contenuto
   (`inspect_pptx_text_colors`, `inspect_thunderbird_prefs`) funzionava correttamente e ha
   cambiato **0 esiti su 9**: quella forma non si replica.
4. **Le coordinate sono parsate come interi, non `eval()`.** L'upstream
   (`mm_agents/.../judge_node`) fa `eval()` su `screencoord`/`size`, che sono stringhe fornite
   dal guest: un percorso di esecuzione arbitraria senza alcun beneficio.
5. **Penalità container.** Un frame che si chiama come la query (una finestra intitolata al
   documento) è il wrong pick classico: la penalità proporzionale all'area fa vincere il widget
   specifico.
6. **Albero dei risultati separato** (`_grounding` in `config.SYSTEM_NAME`). La baseline contiene
   i run su cui poggia ogni numero pubblicato del progetto; un run di questo arm che vi scrivesse
   dentro contaminerebbe esattamente il confronto che l'arm esiste per fare.
7. **Prompt e disponibilità si muovono insieme.** Annunciare un tool che il modello poi trova
   assente è ciò che ha prodotto testo di tool-call fabbricato in
   `docs/finding-confabulation-under-tool-denial.md`.

## 4. Gate pre-registrato

Definito **prima** di eseguire l'arm, in `g10_grounding_signal.GATE_*`:

- **(a)** riduzione relativa ≥ **30%** del tasso di re-click;
- **(b)** almeno **3 task** che guadagnano un SUCCESS.

Entrambe necessarie. Il meccanismo potrebbe plausibilmente migliorare la mira senza muovere gli
esiti: sarebbe un risultato reale sul meccanismo, e comunque non una ragione per scalare.

Campione previsto: 12 task (6 flaky col re-click peggiore, 6 always-fail) + 3 controlli
always-pass, N=3, ~$25-40.

## 5. Risultato: la premessa non regge su Sonnet 5 — arm non lanciato

Il gate ha falsificato la premessa **prima di qualunque spesa di rollout**, che è esattamente
ciò per cui esiste.

Il segnale di targeting era stato misurato su `agent_computer`. Quel tree è
**genuinamente mixed-model**: dei suoi 982 run, **326 serviti da `claude-sonnet-5`, 296 da
`claude-sonnet-4-6`, 5 da `claude-opus-4-8`**, con `model_requested=None` su tutti e 982 — è
l'albero storico non pinnato che `config.py` etichetta come tale, ed è lo stesso gap di
provenance che il progetto ha già documentato. Lì il segnale è reale:

| `agent_computer` (MIXED) | click/run | re-click % | screenshot/click |
|---|---|---|---|
| always-pass | 6.4 | 8.7% | 1.28 |
| always-fail | 7.6 | **11.8%** | 1.71 |
| flaky | 12.5 | **15.7%** | 1.74 |

`z = 2.74`, p < 0.05, e il bucket flaky è l'estremo su tutte e tre le metriche.

Rieseguito su `agent_computer_sonnet5` (882 run, 848 serviti da `claude-sonnet-5`) **scompare, e
il segno si inverte**:

| `agent_computer_sonnet5` (PINNATO) | click/run | re-click % | screenshot/click |
|---|---|---|---|
| always-pass | 5.3 | 4.3% | 0.98 |
| always-fail | 4.6 | **3.4%** | 1.17 |
| flaky | 7.3 | 4.3% | 1.08 |

`z = -1.37`, non significativo, e gli always-fail targettano **meglio** degli always-pass.

Sonnet 5 re-clicca a circa metà del tasso del tree misto e usa meno click per run. Il churn di
mira si legge come una **firma di capacità dei modelli più deboli presenti nel tree misto**
(in buona parte Sonnet 4.6), non come il meccanismo dietro i fallimenti residui di Sonnet 5.

**Conseguenza diretta:** l'harness di grounding sarebbe stato l'intervento giusto per Sonnet 4.6
e non lo è per Sonnet 5. Non viene lanciato. Il codice resta, off per default e con la baseline
verificata byte-identica a flag spento.

### Nota metodologica

Questo è un errore di misura corretto, non una revisione dell'interpretazione: la prima passata
diagnostica ha letto il tree di default (`agent_computer`) invece di quello pinnato. Qualunque
numero di questo progetto calcolato senza `--system`/`OSW_MODEL` esplicito ricade sullo stesso
tree misto e va riverificato prima di essere citato. Sul tree pinnato il pass rate è **56.8%**
(473/833), non 50.8%, e i bucket sono 137/36/104 invece di 104/46/102.

## 6. Cosa renderebbe l'arm sensato

- Misurare un **modello più debole** (Sonnet 4.6 è già sul disco nel tree misto): lì il gap esiste
  e l'arm avrebbe una premessa. Utile per la tesi come contrasto fra generazioni di modello.
- Un segnale di targeting che **riemerga** su un sottoinsieme di task del tree pinnato (es. una
  singola app): `g10_grounding_signal` lo direbbe, ma su 277 task non c'è.
- Una riformulazione che non dipenda dal re-click come proxy: per esempio il tasso di
  `no_effect` misurato direttamente da `click_element`, che l'arm stesso produrrebbe — però
  ottenerlo richiede già di far girare l'arm.

## 7. File

```
benchmarks/osworld/grounding.py                        logica pura (24 test)
benchmarks/osworld/mcp/grounding_tools.py              i tre tool (23 test)
benchmarks/osworld/mcp/server.py                       gate OSW_GROUNDING
benchmarks/osworld/prompts.py                          GROUNDING_LINES / GROUNDING_BLOCK
benchmarks/osworld/runners/agent_computer.py           _allowed_tools, _grounding_telemetry
benchmarks/osworld/config.py                           GROUNDING*, suffisso SYSTEM_NAME
benchmarks/osworld/analysis/g10_grounding_signal.py    metrica e gate (10 test)
core/run.py                                            OSW_GROUNDING* in harness.json
```

Esecuzione, se mai servisse:

```bash
# misura il segnale su un tree (sempre con --system esplicito, vedi §5)
python -m benchmarks.osworld.analysis.g10_grounding_signal --system agent_computer_sonnet5

# confronto e gate
python -m benchmarks.osworld.analysis.g10_grounding_signal \
    --system agent_computer_sonnet5 --compare agent_computer_sonnet5_grounding

# arm
OSW_MODEL=claude-sonnet-5 OSW_GROUNDING=1 python -m core.run --benchmark osworld ...
```
