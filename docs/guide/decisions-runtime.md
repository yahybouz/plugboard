# Runtimes locaux de décision

Depuis la décision 72 du 24 octobre 2026, seuls `laya` (anglais) et
`laya-multilingual` composent le runtime local hébergeable. Jev utilise l'API
TypeSafe distante, sans poids locaux. Kev est retiré du produit actif.

Laya utilise son implémentation officielle de décision, dans un
interpréteur ML distinct du gateway. Aucun paquet ML n'est ajouté aux dépendances
ordinaires. Une requête démarre un processus privé à usage unique, communique par
stdin/stdout et termine ce processus après sa réponse. Aucun port HTTP ni bind
public n'est ouvert. Le démarrage à froid et la vérification des fichiers font
partie du délai worker de 30 secondes (`limits.timeout_seconds`). L'enveloppe
API de 45 secondes (`limits.api_timeout_seconds`) est distincte : elle couvre
dispatch, annulation et réponse, sans prolonger les 30 secondes de calcul local.
Ces choix privilégient l'isolation pour ce premier
chemin local ; aucune latence de service chaud n'est revendiquée.

## Provenance vérifiée

[Le manifest](../../manifests/decisions.json) contient, pour chaque fichier, une URL
publique à révision fixe, la taille et le SHA256. Les sources officielles sont
également épinglées et vérifiées avant leur import. Les licences et métadonnées
amont sont conservées parmi les fichiers téléchargés.

| Moteur | Checkpoint HF | Source officielle | Dépendance réellement chargée |
|---|---|---|---|
| Laya anglais | `convaiinnovations/laya@7b928d828b7b0e022f929d9bd2e44165aa270148` | `NandhaKishorM/laya@0a05a45052e23ae0b887a490fe8d6d9c8cff2ab5` | `model.safetensors` contient encodeur complet, tête décision et tête action ; configurations encodeur et tokenizer locaux |
| Laya multilingue | `convaiinnovations/laya-multilingual@1720e3e3357cfe1e281542e223f8273b0890ca34` | même révision Laya | mêmes composants, checkpoint multilingue complet |

Lectures primaires : [Laya Agent](https://github.com/NandhaKishorM/laya/blob/0a05a45052e23ae0b887a490fe8d6d9c8cff2ab5/laya/agent.py) et
[Laya build_model/build_head](https://github.com/NandhaKishorM/laya/blob/0a05a45052e23ae0b887a490fe8d6d9c8cff2ab5/laya/common.py).

Laya construit l'encodeur depuis sa configuration locale puis charge strictement
le checkpoint complet (`pretrained=False`). Les noms ModernBERT/mmBERT dans sa
configuration décrivent l'origine de l'encodeur ; aucun autre poids de base n'est
nécessaire à ce chemin. Aucun classifieur générique ni génération de
texte ne remplace ce chemin. Le correctif officiel de configuration tokenizer
Laya est appliqué dans une copie temporaire des petites configurations ; poids et
JSON tokenizer sont des hardlinks. Le snapshot vérifié reste intact, y compris en
multilingue. Le gateway possède le répertoire temporaire et le supprime après le
kill du worker, même si ce dernier ne peut pas exécuter son `finally`.

## Installation et téléchargement explicites

Ces commandes préparent explicitement le runtime ML optionnel, distinct des
paquets du gateway. Le lock courant cible **Python 3.12 / macOS arm64 ≥14** :
34 paquets épinglés avec hashes, dont [Torch 2.14.1](https://pypi.org/project/torch/2.14.1/).
La fermeture a été réduite hors ligne à partir des métadonnées des roues du lock
précédent pour Darwin arm64 / Python 3.12. Les versions et hashes conservés sont
inchangés. La source Laya officielle épinglée utilise `torch`, `transformers`,
`safetensors`, `huggingface-hub` et `numpy` ; ses helpers structurés optionnels ne
font pas partie du worker CPU. Les dépendances exclusivement Kev ont été retirées.

**Le lock Laya seul n'a pas été installé dans un nouvel environnement.** Les
preuves d'installation et de smoke de l'ancien lock ne le qualifient pas. Le
retrait de Kev ne justifie aucun retour à Torch 2.8.0. Installer seulement dans
un nouvel environnement isolé, sans modifier `ml-v4-env` ni les venv partagés.

**Portée de plateforme :** les wheels Torch de ce lock exigent macOS ≥14 ;
macOS 13 n'est pas compatible avec ce runtime optionnel. La présence de hashes
Linux/Windows ne rend pas la résolution universelle : ce lock est résolu pour
macOS arm64. Il faut une résolution et une installation/smoke Linux spécifiques
avant tout hébergement ML sur OVH. Aucun support multi-plateformes ni capacité du
VPS existant n'est revendiqué ; les fonctions ordinaires du gateway n'acquièrent
pas ces dépendances ML.

```sh
# Depuis la racine du worktree. Aucun environnement partagé ou production.
export DECISION_TEST_ROOT="${TMPDIR:-/tmp}/plugboard-decision-test"
mkdir -p "$DECISION_TEST_ROOT"
uv venv --python 3.12 "$DECISION_TEST_ROOT/ml-laya-only-env"
uv pip install --python "$DECISION_TEST_ROOT/ml-laya-only-env/bin/python" \
  --require-hashes -r requirements-decisions.lock

# Préflight sans poids : le budget refuse avant tout accès réseau.
.venv/bin/python scripts/decision_download.py laya \
  --assets "$DECISION_TEST_ROOT/assets" --max-file-mb 100
# Attendu : download_budget_exceeded, code 1.

# Téléchargement complet explicite, après examen de l'espace disponible.
.venv/bin/python scripts/decision_download.py laya \
  --assets "$DECISION_TEST_ROOT/assets"
.venv/bin/python scripts/decision_download.py laya \
  --assets "$DECISION_TEST_ROOT/assets" --check

export PLUGBOARD_DECISION_PYTHON="$DECISION_TEST_ROOT/ml-laya-only-env/bin/python"
export PLUGBOARD_DECISION_ASSETS="$DECISION_TEST_ROOT/assets"
export PLUGBOARD_SKIP_DOTENV=1
.venv/bin/python scripts/decision_smoke.py laya
```

Remplacer l'identifiant par `laya-multilingual` pour préparer l'autre
checkpoint. Taille des fichiers épinglés : environ 847 MB pour l'anglais et
680 MB pour le multilingue, hors environnement Python. Le téléchargement réserve au moins
6 GiB libres en plus de tous les fichiers demandés. Il exige une destination
privée (mode 0700), refuse les symlinks, vérifie taille/hash en streaming, puis
publie atomiquement un marqueur lié au manifest. Un téléchargement interrompu ne
rend pas le modèle disponible. Les poids restent dans ce répertoire jetable.

La disponibilité du catalogue vérifie configuration, marqueur, présence et tailles
sans démarrer de ML. La vérification intégrale des hashes se fait dans le worker,
avant tout import des sources téléchargées et toute désérialisation du checkpoint.
`ready` est un état de préparation des fichiers, pas une preuve d'inférence ou de
compatibilité du paquet Python. La commande `--check` force aussi tous les hashes.

## Bornes et sémantique

Le worker n'hérite d'aucune clé, proxy, configuration provider ou `PYTHONPATH` du
gateway. HF et Transformers restent offline ; les connexions socket sont refusées.
Aucun téléchargement n'est déclenché à l'inférence. Le code distant HF n'est pas
activé. Le worker utilise CPU et Torch ; ce chemin ne mesure pas les
performances MPS/MLX. Une machine dont la RAM physique détectée est inférieure à
8 GiB est refusée. Cela ne certifie pas que le modèle tient en mémoire ni qu'un
VPS de 4 GiB serait compatible.

Un verrou interprocessus dans le répertoire d'assets et l'admission du gateway
limitent à un worker à la fois pour ce répertoire, tous modèles confondus. Dans
un processus, le gateway fait attendre les autres clés dans une file FIFO (8
places, 15 secondes au plus, puis 429) ; une même clé n'a qu'une décision en vol.
Un appel venu d'un autre processus sur le même répertoire est refusé
`worker_busy`. Laya est gratuite : chaque décision réussie est tracée dans
l'usage de la clé (`decision:laya`, coût 0, réponse
`{"status":"billed","cost_usd":0.0}`, source `builtin:laya-free`). Jev est
facturé au prix officiel TypeSafe, 0,042 $ par million de tokens d'entrée
(sortie gratuite), sur les tokens que sa réponse déclare (`usage.input_tokens`) ;
usage absent ou invalide : plafond de 64 000 tokens, statut `estimated`. Voir
[Typed decisions](decisions.md) pour le détail et le budget. L'attente en file
est décomptée du délai moteur : file et moteur tiennent dans les 45 secondes
publiées. Le processus et son groupe sont tués lors d'un
timeout, d'une annulation, d'une sortie excessive ou après leur réponse. Stdin est
limité à 64 KiB, stdout à 128 KiB ; stderr est jeté et les exceptions exposent
uniquement des codes fixes. Le délai inclut démarrage à froid et hashing.

Laya applique ici les limites des checkpoints : 512 tokens anglais et 1 024 tokens
multilingues. Le vrai tokenizer vérifie l'état, les instructions et toutes les
options avant chargement des poids. Les caps internes d'options (48 tokens), le
budget de tête (**192 anglais, 256 multilingue**) et l'espace état+question sont
vérifiés pour refuser une requête qui
serait tronquée par l'implémentation amont. Le mode multilingue long 8 192 tokens
reste hors de ce chemin.
Les limites globales API (4 000 octets d'état, 8 questions, 16 options) restent
cumulatives et ne garantissent pas l'admission tokenizer.

Le catalogue dérive les budgets de tête/contexte des copies locales de
`rl_agent_config.json` dans `manifests/decision-configs/`, dont taille et SHA256
sont vérifiés contre le manifest épinglé. Les snapshots téléchargés et leurs
marqueurs restent inchangés. `head_tokens` et `question_branch_tokens` décrivent
la tête totale, pas un plafond indépendant pour les instructions. Les options et
leurs séparateurs consomment `sum(1 + tokens(option))` ; le préfixe de type fait
partie des instructions tokenisées. Le worker contrôle l'espace restant pour
chaque question. `instruction_tokens` vaut donc `null` dans le catalogue.

`noul` représente P(vrai). `score` est l'espérance des indices ordinaux commençant
à zéro, avec légende et distribution. Laya conserve sa confiance d'entropie
`1-H(p)/log(K)` ; elle n'est pas P(correct). L'implémentation officielle
arrondit les probabilités à quatre décimales. Le worker
renormalise seulement un résidu de somme <=0,001, puis recalcule l'espérance si
l'écart reste dans la borne de quantification dérivée
`0,00005*(1+sum(indices)) + abs(somme-1)*abs(espérance_normalisée)`.
La confiance amont reste inchangée. Les grandes corruptions ne sont pas réparées et
la validation de sortie du gateway les refuse.

## Preuves et essais restants

`tests/test_decision_assets.py` utilise de minuscules fichiers et un transport
HTTP simulé. `tests/test_decision_runtime.py` démarre réellement des processus
Python synthétiques pour vérifier environnement, admission, timeout, kill,
annulation et limites ; ses ports ML officiels sont simulés. Ces tests ne
constituent aucune inférence Laya. Le script smoke ci-dessus est réservé à
la vraie inférence officielle et vérifie des cas synthétiques positif, négatif et
ordre inversé des options avec les trois primitives. Son JSON rapporte réponses,
latence incluant chargement et maximum RSS des processus enfants ; chaque échec de
label reste un échec, sans remplacement du résultat.

Les preuves ci-dessous précèdent la décision 72 et ne qualifient pas le
candidat Laya seul. Elles restent conservées, y compris celles de Kev désormais
supersédées ; aucun poids, cache ni rapport historique n'est effacé. Les anciens
identifiants Kev sont seulement refusés par le garde chat, sans capacité runtime
ou téléchargement.

La première passe producteur n'avait exécuté aucune ML. Le parent a ensuite
préparé les modèles complets, vérifié leurs hashes et exécuté les inférences
natives ainsi que six appels HTTP et trois MCP réels, tous 200 (preuves
`*-native-proof.json` et `real-api-mcp-v1.json`). Les smokes inchangés donnent
Laya anglais 3/3, Kev 3/3, **Laya multilingue 2/3 avec échec réel du cas négatif**.
Ce résultat reste une réserve qualité, sans changement des fixtures ni seuils.
Un chargement natif Laya avait modifié `tokenizer_config.json` multilingue ; le
parent a réparé et revérifié les assets avec le downloader. Ce n'était pas une
mutation du worker, qui répare uniquement sa copie de staging.

Le correctif F1–F3 ne relance pas d'inférence. Jev sans clé reste indisponible
(503), sans validation réelle Jev. Ces preuves
limitées ne certifient ni qualité française ni sécurité globale ; les revues
finales relèvent du parent.

La preuve parent `real-api-mcp-v2-isolated.json` ajoute la vérification isolée
de six appels HTTP, trois MCP et deux SDK réels, sondes Ollama de boot désactivées.
Le correctif SPEC v2 des préfixes/discovery et sondes n'a pas relancé d'inférence
et ne change ni les délais 30/45 secondes ni la réserve multilingue négative.

## Remédiation Torch historique et réserve de désérialisation

Avant la décision 72, le runtime isolé Torch 2.14.1 avait été installé depuis
le lock historique de 41 paquets,
`pip check` sans erreur et audit de versions sans avis retourné à la date du
24 octobre 2026, sans exclusion. Ne pas transformer ce résultat en quitus global.
Les smokes inchangés sur sources figées restent **Laya 3/3, Kev 3/3 et multilingue
2/3**. L'échec négatif multilingue n'est pas corrigé par la montée de Torch.

**CVE-2026-4538 / PYSEC-2026-139 reste une réserve ouverte** : l'avis consulté
n'annonce aucun correctif et sa plage s'arrête à `last_affected:2.10.0`.
L'absence de correspondance 2.14.1 dans le scanner ne prouve donc ni une correction
ni une version 2.14.1 affectée. Les mécanismes de chargement pt2/pickle concernés
restent visibles dans le code Torch installé ; aucun PoC n'a été exécuté.

Les probes historiques Laya passent par `safetensors`. Le chemin Kev supprimé
chargeait `head.pt` par `torch.load` ; sa suppression ne certifie ni Torch en
général ni les branches pt2. Aucun appel
`torch.export.load` / `torch.jit.load` n'a été observé dans ces probes, sans
prétendre couvrir toutes les branches tierces. Le corps client n'accepte ni
checkpoint, ni fichier, ni URL, ni chemin : conserver cette frontière, les
hashes/provenances et les droits privés du store. Cela ne garantit pas la sûreté
d'un pickle ni d'une chaîne d'approvisionnement compromise. Ne pas accepter
d'artefact pt2 ou checkpoint fourni par un client pour contourner la réserve.


La présente révision n'établit aucune validation UI, pentest, revue SPEC/sécurité
ou autorisation de publication. Les preuves V4 restent rattachées à V4 ; aucun
nouveau GO Linux/OVH n'en découle.
