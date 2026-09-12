# Piano di implementazione: harness di grounding (Mixture-of-Grounding) per OSWorld + Sonnet 5

**Stato:** implementato e testato — **non lanciato; fix del canale a11y scritto ma NON validato** (vedi §8, §9), e la premessa statistica non regge su Sonnet 5 pinnato (§5)
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


## 8. Causa decisiva: l'albero di accessibilità è vuoto, e lo è sempre stato

Verificato su **tutte le 456 catture reali di `a11y_tree` presenti su disco** — ogni albero dei
risultati (`agent_computer`, `agent_computer_sonnet5`, `agent_computer_astra`,
`verify_replan_sonnet5`), tutte le 9 app, entrambe le campagne:

```
captures: 456    vuote (<= 1 elemento): 456 (100.0%)    popolate: 0
envelope: {"AT": "<desktop-frame xmlns:.../>"}   <- root autochiudente, zero figli
```

Per app, sul tree pinnato: chrome 4, gimp 14, libreoffice_calc 13, libreoffice_impress 11,
libreoffice_writer 4, os 24, thunderbird 23, vlc 41, vscode 4 — **tutte vuote, nessuna popolata**.

L'immagine guest installa `at-spi2-core` e `python3-pyatspi`
(`benchmarks/osworld/docker/Dockerfile.osworld:54`), quindi la dipendenza c'è, ma **nessun bridge
AT-SPI sta effettivamente producendo un albero a runtime** (serve il bus di accessibilità attivo e
i bridge dei toolkit abilitati per GTK/Qt: `toolkit-accessibility`, `GTK_MODULES=gail:atk-bridge`,
`QT_ACCESSIBILITY=1`).

### Conseguenze, in ordine di importanza

1. **L'harness di grounding non è valutabile**, su Sonnet 5 né su nessun altro modello, finché
   l'immagine non è riparata. Risolverebbe zero elementi e cadrebbe sempre sul fallback visivo.
   Correttamente: `find_element`/`click_element` riportano che il bridge non riporta nulla e
   `click_element` **non clicca** su coordinate indovinate (test dedicato).
2. **Corregge la motivazione originale di questo arm.** Lo 0.8% di uso di `a11y_tree` non è un
   canale strutturato trascurato a favore dei pixel: l'agente l'ha chiamato 456 volte, non ha
   ricevuto niente, e ha smesso. Il comportamento era razionale.
3. **Chiude il dibattito dell'idea #2 del backlog** («a11y-first vs grounding visivo»): in questo
   harness a11y-first non è mai stato disponibile. La discussione era priva di oggetto.
4. **Ridimensiona retroattivamente due risultati già chiusi.** L'auditor di Verify-Replan aveva
   `a11y_tree` fra i suoi tool e la sua "gerarchia delle evidenze" citava a11y come fonte: era di
   fatto **solo-screenshot**. Lo stesso vale per l'idea #15. La "cecità da screenshot" diagnosticata
   come meccanismo dei loro risultati nulli aveva quindi una seconda componente non rilevata — non
   solo «lo screenshot non vede il contenuto dei file», ma anche «il canale strutturato era morto».
   Non cambia i verdetti (entrambi chiusi come nulli), cambia la spiegazione.
5. **Prerequisito per qualunque intervento basato su a11y**: riparare il bridge nell'immagine e
   riverificare con `find_element` / `list_elements` su un task reale. Solo dopo il gate §4 ha
   senso.

### Bug corretti in questa passata

- `Controller.a11y_tree()` restituisce **`{"AT": "<xml>"}`**, non XML grezzo. `parse_elements`
  assumeva XML grezzo: ogni chiamata live avrebbe risposto «did not parse as XML». Risolto con
  `grounding.unwrap_tree()`, che accetta envelope, doppio envelope del transport MCP e XML grezzo.
- `_tree()` distingue ora «root senza figli» da «XML malformato», perché il primo è l'unico caso
  che si verifica live e richiede un messaggio diverso.
- I byte reali dell'envelope catturato sono fissati come fixture (`REAL_EMPTY_ENVELOPE`), così
  entrambe le assunzioni sbagliate restano sotto test.


## 9. Riparazione del canale AT-SPI (scritta, non validata)

### Cosa è stato cambiato

`docker/start.sh` — il guest non aveva **nessun bus D-Bus di sessione**, da cui si bootstrappa il
bus di accessibilità: `Xvfb` + `openbox` + server Flask, e nient'altro. Aggiunto, **prima** di
openbox e delle app (un bridge di toolkit si registra alla costruzione dei widget: un'app avviata
prima che il bus esista resta invisibile ad AT-SPI per tutta la sua vita, anche se il bus arriva
dopo):

- `dbus-launch --sh-syntax` con export di `DBUS_SESSION_BUS_ADDRESS`;
- `at-spi-bus-launcher --launch-immediately` e `at-spi2-registryd`, con **path risolto** fra
  `/usr/libexec`, `/usr/lib/at-spi2-core` e `PATH` — un path hardcoded che sbaglia fallirebbe in
  silenzio e sarebbe indistinguibile dal bug che stiamo correggendo, quindi in assenza si logga
  un WARNING esplicito;
- lato toolkit: `GTK_MODULES=gail:atk-bridge`, `GTK_A11Y=atspi`, `NO_AT_BRIDGE=0`,
  `QT_ACCESSIBILITY=1`, `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`, più
  `gsettings set org.gnome.desktop.interface toolkit-accessibility true` (tollerante al fallimento:
  senza un demone dconf scrivibile non riesce, e le env var portano già lo stesso interruttore);
- `SAL_USE_VCLPLUGIN=gtk3`.

`Dockerfile.osworld` — aggiunto **`libreoffice-gtk3`**. LibreOffice espone AT-SPI solo attraverso
il backend VCL gtk3; col plugin X11 generico non riporta nulla, il che lascerebbe cieche le tre
famiglie `libreoffice_*` — la fetta più grande del task set — anche con un bus funzionante.

### Perché la patch non basta da sola

La disciplina del progetto è già scritta nel Dockerfile: *«an apt-get install claim isn't
validation, a live check is»*. Qui la sonda è parte della consegna:

- `grounding.tree_health(raw)` distingue i tre fallimenti che dall'esterno si somigliano —
  controller irraggiungibile/vuoto, XML malformato, **root senza figli** (l'unico che questo
  harness produce) — e in più segnala l'albero che riporta nodi senza geometria utile.
- `common._a11y_health(ctrl)` scrive `a11y_ok` / `a11y_nodes` / `a11y_elements` / `a11y_reason` in
  **ogni `result.json`**. Una sonda per run, mai solleva, mai blocca. È ciò che rende la
  riparazione verificabile dai dati invece che creduta, e rende visibile una regressione al primo
  run invece che dopo mille.
- `agent_computer._grounding_precheck(ctrl)` **rifiuta di avviare l'arm** se il canale non riporta,
  scrivendo un ENVIRONMENT_ERROR che dice come procedere. Solo con l'arm acceso: la baseline lavora
  da screenshot e non va mai fatta fallire per un albero vuoto. Con l'arm acceso invece è
  necessario, perché un ambiente rotto si leggerebbe come meccanismo nullo — la confusione che è
  già costata un'analisi intera.

### Stato e prossimo passo

**Non validato.** Nessuna di queste righe ha girato contro un'immagine ricostruita; non ho un
sandbox Daytona attivo né accesso al registry, e ricostruire e pubblicare l'immagine è un'azione
che tocca l'infrastruttura condivisa. Sequenza:

1. `docker build --platform linux/amd64 -f benchmarks/osworld/docker/Dockerfile.osworld ...`
2. push, `docker inspect ... RepoDigests`, re-pin di `OSW_IMAGE`/`config.IMAGE`
3. un singolo task reale: se `result.json` riporta `a11y_ok: true` con `a11y_elements > 0` il canale
   è vivo; altrimenti `a11y_reason` dice quale dei quattro modi ha fallito
4. solo allora il gate §4 ha senso, e `list_elements()` su un task Calc dice se le celle sono
   esposte per riferimento (l'assunzione su cui poggia la nicchia di §5)

### Avvertenza metodologica

Un'immagine ricostruita è una **versione diversa dell'harness**: il suo pass rate non è
direttamente confrontabile con l'albero `agent_computer_sonnet5` da 882 run. Va trattata come nuova
baseline, con la stessa disciplina già applicata a `provenance.evaluator_commit`. In più le URL
`.deb` di Chrome e VSCode sono "current stable", quindi un rebuild porta anche versioni più nuove
di entrambi.
