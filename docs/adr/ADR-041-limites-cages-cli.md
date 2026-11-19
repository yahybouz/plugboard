# ADR-041 : limites connues des cages CLI (codex, claude, gemini)

## Statut

Accepté (19/11/2026), release 4.15.2, PLU-100. Cet ADR nomme des limites et leurs bornes. Il ne change aucun code.

## Contexte

Le rejeu P0-5 « cage hostile » (18/11, image UAT 4.14.6, conteneur en `apparmor=plugboard-userns (enforce)`) a lancé une charge hostile en Python à la place du CLI, sous trois régimes : la cage agy, le bac à sable de codex et un lancement sans cage. Le lancement sans cage est celui de claude et de gemini. 4.15.1 a fermé le réseau de la cage agy (BUG-156), et 4.15.2 lui donne un store privé par appel (BUG-157). Il reste trois lignes, communes à plusieurs régimes, et le cas de claude et de gemini.

| id | Geste hostile | Cage agy | Bac à sable codex | Sans cage (claude, gemini) |
|---|---|---|---|---|
| R4 | lire une variable d'environnement qui porte un secret | BLOQUÉ | **PASSE** (`OPENAI_API_KEY`) | **PASSE** (`ANTHROPIC_API_KEY`) |
| W7 | déposer un binaire dans l'espace puis l'exécuter | PASSE | PASSE | PASSE |
| M3 | `unshare` imbriqué (userns + mountns), puis `mount` | PASSE | PASSE | PASSE |
| R1–R12, W1–W6, X1–X8, M2, M8 | lire ou écrire hors de l'espace, voir les autres sessions, la passerelle | BLOQUÉ (sauf R8, W5, W6, X3 : BUG-157) | BLOQUÉ | **PASSE** |

Ce que chaque CLI reçoit en 4.15.2 (`plugboard/cli_subprocess.py`) :

| CLI | Cage OS | Ce que le modèle peut exécuter | Environnement | Réseau |
|---|---|---|---|---|
| codex | Bac à sable **de codex** (son bwrap, son seccomp), réglé par `codex_isolation_args` : profil `plugboard_chat`, `":root"="deny"`, `":minimal"="read"`, `/etc` refusé, seul le dossier de lancement est lisible (écriture avec `PLUGBOARD_CODEX_SANDBOX=workspace-write`). Fonctions coupées (`apps`, `plugins`, `hooks`, `multi_agent`…), `--ignore-user-config --ignore-rules --ephemeral` | Des commandes : c'est l'outil shell de codex | Liste blanche `build_child_env("codex")` : `CODEX_HOME`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, plus la base commune | Coupé par le seccomp de codex (`socket() EPERM`) |
| claude | **Aucune**, même uid que la passerelle | Rien : `--tools ""`, MCP strict, aucune source de réglages, pas de commandes slash. Pour une requête web : `WebSearch` et `WebFetch` seulement (ADR-040) | `ANTHROPIC_*`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, plus la base commune ; `HTTPS_PROXY` vers l'egress web pour une requête web | Celui du conteneur. Pour une requête web, l'egress web (adresses publiques, port 443) via `HTTPS_PROXY` ; PLU-162 met ce run dans une cage réseau |
| gemini | **Aucune**, même uid que la passerelle | Rien : `tools.core: []`, politique admin « deny all », `-e none`, aucun serveur MCP, `GEMINI_CLI_HOME` privé par appel | `GEMINI_*`, `GOOGLE_*`, plus la base commune | Celui du conteneur |

Dans tous les cas, les secrets du serveur (`ADMIN_KEY`, `API_KEY`, `ENCRYPTION_KEY`, `PLUGBOARD_*`…) ne passent jamais à un CLI (`_NEVER_FORWARD_ENV`).

## Instruction

### a) Le secret de codex dans l'environnement (R4)

- **Ce qui est mesuré.** La charge est lancée par `codex sandbox` avec `codex_isolation_args` (en `workspace-write`) et l'environnement de `build_child_env("codex")`. Une fausse `OPENAI_API_KEY` y a été ajoutée : elle est absente du `runtime.env` de l'UAT. La commande la lit.
- **Où est la barrière.** Le bac à sable de codex cache `~/.codex/auth.json` (R7 BLOQUÉ) et le `/proc` du processus codex (R2, R3 : `ENOENT`, espace de pid propre). C'est l'espace de montage du bac à sable qui protège, pas l'uid. Une clé rangée dans le `auth.json` d'un `CODEX_HOME` privé, hors du dossier de lancement, serait donc cachée aux commandes. C'est la différence avec agy (R8) : agy est tout entier dans sa cage et doit lire son jeton.
- **Gain réel ou nul.** Le gain est **réel seulement si une clé est dans l'environnement de la passerelle**. Dans ce cas, un client obtient la clé de l'opérateur dans la réponse : le réseau du bac à sable est coupé, la réponse est la seule sortie. Le gain est **nul pour les installations actuelles** : en UAT et en prod, codex est connecté par un compte ChatGPT (`codex login`, `~/.codex/auth.json`, R7 BLOQUÉ) et aucune `OPENAI_API_KEY` n'est dans l'environnement. R4 n'y expose rien.
- **Ce qui n'est pas établi.** codex 0.163.0 (version de l'image) a un réglage `shell_environment_policy` qui fixe l'environnement des commandes qu'il lance (`inherit`, `exclude`, `include_only`, `ignore_default_excludes`, `set`, lus dans le binaire). Deux points restent ouverts : sa valeur par défaut, et le fait que `codex sandbox` (le lanceur de la maquette) l'applique comme `codex exec`. La clé est peut-être déjà retirée des commandes d'un vrai chat. Seul un vrai `codex exec` dont le modèle lance `env` le prouve. C'est une inférence réelle, à faire sur l'UAT.

### b) Exec dans l'espace (W7) et `unshare` imbriqué (M3) sous codex

- **W7.** Exécuter des commandes est la fonction de l'outil shell de codex. Le shell et les binaires de l'image, lisibles par `":minimal"`, sont déjà exécutables. Un binaire déposé n'ajoute donc pas de capacité nouvelle : il ouvre seulement du code natif choisi par l'attaquant. La passerelle contrôle ce que la commande voit (profil de fichiers), l'écriture (lecture seule par défaut pour les chats, tmpfs éphémère : W2 et W4 « ABSENT hors cage ») et le réseau (coupé). Elle ne monte pas l'espace de codex : c'est le bwrap de codex qui le fait. Aucun réglage connu du profil ne le monte en `noexec`.
- **M3.** Le bwrap de codex 0.163.0 ne passe pas `--disable-userns` (la chaîne est absente du binaire), et la cage agy non plus (`agy_cage.cage_argv`). Le bac à sable de codex et la cage agy créent eux-mêmes un espace utilisateur. Le seccomp du conteneur permet donc `unshare`, `clone`, `mount` (`seccomp-plugboard-userns.json`, prod `codex-seccomp.json`). Un espace imbriqué ne montre rien de neuf : il remonte ce que la cage voit déjà. Ce qu'il ouvre, c'est la surface du noyau atteignable sans privilège depuis un espace utilisateur. La passerelle ne peut pas l'interdire sous codex sans casser le bac à sable de codex.

### c) claude et gemini sans cage OS

- **Hors web, aucun des deux n'a d'outil.** Le modèle ne peut ni lire un fichier, ni lancer une commande, ni ouvrir une connexion. Les lignes « sans cage » mesurent ce qu'obtiendrait du code arbitraire lancé comme eux : `ADMIN_KEY` par `/proc/<passerelle>/environ` (R3), les clés de `data/` (R5), l'écriture du code (W3), les autres sessions (X1–X8). C'est ce qu'hériterait un harness qui leur rendrait des outils. Aujourd'hui, le modèle n'a pas de chemin vers ces lignes.
- **Run web de claude.** Il a deux outils en lecture (`WebSearch`, `WebFetch`), un nombre de tours borné et l'egress web. En 4.15.1, cet egress n'était qu'un `HTTPS_PROXY` : un client qui l'ignore sort par le réseau du conteneur. PLU-162 (4.15.2) met le run dans une cage réseau, avec un egress par run. Sa preuve est le rejeu « client qui ignore `HTTPS_PROXY` bloqué ».
- **gemini.** Il n'a aucun outil, deux fois : liste vide et politique admin « deny all ». Ses réglages, sa mémoire et ses politiques d'opérateur sont remplacés à chaque appel. Il n'a pas de web.

## Décision

1. **Pas de changement de code en 4.15.2** pour R4, W7 et M3. Ces trois lignes sont des limites acceptées, avec les bornes ci-dessous.
2. **Une clé OpenAI ne se met pas dans l'environnement de la passerelle pour codex.** On connecte codex avec `codex login`, avec un compte ChatGPT ou `--with-api-key`. La clé est alors écrite dans `~/.codex/auth.json`, que le bac à sable cache (R7). `OPENAI_API_KEY`, `CODEX_API_KEY` et `CODEX_ACCESS_TOKEN` restent dans la liste blanche de codex pour ne pas casser une installation qui s'en sert. Ces installations héritent de R4.
3. **claude et gemini restent sans cage OS tant qu'ils n'ont aucun outil.** La seule exception est le run web de claude, dont le réseau est encagé par PLU-162.
4. **Pour un harness d'agents** (gelé, D-75), ces limites ne sont pas acceptées. La liste est plus bas.

## Limites acceptées et leurs bornes

| Limite | Bornée par | Reste ouvert |
|---|---|---|
| R4 codex : une clé d'environnement est lisible par les commandes | aucune clé dans l'environnement en UAT et en prod ; réseau du bac à sable coupé ; `/proc` de codex invisible ; secrets du serveur jamais transmis | une installation qui met `OPENAI_API_KEY` dans l'environnement expose cette clé au client |
| W7 codex, agy, sans cage : un binaire déposé s'exécute | profil de fichiers (dossier de lancement seul) ; lecture seule par défaut ; tmpfs éphémère ; réseau coupé (codex) ou egress Google seul (agy, BUG-156) ; limites du conteneur (`pids_limit` 256, `mem_limit` 3 Go ; E1–E3 BORNÉ) | du code natif choisi par l'attaquant tourne dans la cage |
| M3 codex, agy, sans cage : `unshare` imbriqué | `cap_drop: ALL`, `no-new-privileges` (M5), setuid 0 refusé (M4), pas de socket Docker (M6), `chroot` refusé (M7), AppArmor et seccomp du conteneur | la surface du noyau atteignable depuis un espace utilisateur : mises à jour du noyau de l'hôte (veille sécurité, hors dépôt) |
| R8 agy : le jeton de l'appel est lisible | copie privée et jetée à la fin de l'appel (BUG-157) ; egress limité aux hôtes Google d'agy (BUG-156) | un canal vers Google reste (voir [la cage d'agy](../guide/providers-and-subscriptions.md#agys-cage)) |
| claude et gemini sans cage OS | aucun outil hors web ; run web limité à deux outils en lecture, tours bornés, egress web, cage réseau (PLU-162) ; environnement en liste blanche | aucun cloisonnement de fichiers ni de processus si un outil leur était rendu |

Ces bornes tombent avec les réglages qui les élargissent : `PLUGBOARD_CODEX_SANDBOX=danger-full-access` (aucun bac à sable), `PLUGBOARD_SUBPROCESS_CWD` (le dossier visé devient lisible), `PLUGBOARD_SUBPROCESS_ENV_PASSTHROUGH` (variables transmises en plus). Les outils émulés de l'ADR-039 ne changent rien : c'est le client qui les exécute, pas le CLI.

## Ce qui serait interdit pour un harness d'agents

Un harness rend des outils (shell, fichiers, MCP) au modèle. Il est gelé (D-75). S'il est dégelé, sont interdits :

- un CLI à outils **sans cage OS** : claude et gemini tels qu'aujourd'hui ;
- un agent avec **le même uid que la passerelle**, ou qui voit son `/proc`, son environnement, `data/` ou le code ;
- une **clé fournisseur dans l'environnement** de l'agent (R4) : jeton délégué à durée courte, ou appel par la passerelle ;
- le **réseau du conteneur** : seul un egress par session, avec liste d'hôtes ;
- un **espace de travail partagé** entre sessions, ou un store de CLI partagé (BUG-157) ;
- des épuisements bornés seulement par les limites **globales** du conteneur : il faut un cgroup par session ;
- un `unshare` imbriqué permis (M3) sans décision explicite : `--disable-userns` dans une cage dont la passerelle tient le bwrap ;
- l'exécution dans l'espace (W7), sauf si l'agent doit compiler : l'espace est alors monté `noexec` par défaut.

Le critère de sortie reste celui de l'étude P0-5. Au banc rejoué, toutes les lignes sont BLOQUÉ, sauf W0 (témoin) et éventuellement W7. E1–E3 sont bornés par le cgroup de la session.

## Conditions de révision

Cet ADR est revu, ou remplacé par un nouvel ADR, quand l'un de ces cas se présente :

- le harness est dégelé (D-75 levée) ;
- une installation doit passer une clé OpenAI à codex par l'environnement. Il faut d'abord traiter R4 : `shell_environment_policy` ou un `CODEX_HOME` privé, prouvé par un vrai `codex exec` ;
- `CODEX_VERSION` change dans le `Dockerfile`, ou codex change son bac à sable (`--disable-userns`, `noexec`, environnement des commandes). On rejoue R4, W7 et M3 ;
- un outil est rendu à claude ou à gemini, hors les deux outils web ;
- PLU-162 n'est pas livré, ou son rejeu montre qu'un run web sort sans l'egress ;
- l'hôte change de noyau ou de politique AppArmor sur les espaces utilisateur sans privilège.

## Conséquences

- Les trois lignes de PLU-100 sont nommées et bornées, au lieu de rester des « PASSE » sans statut au rejeu P0-5.
- Une installation qui met une clé OpenAI dans l'environnement garde R4. La décision 2 le lui dit.
- Le rejeu P0-5 de fin de release doit toujours montrer R4, W7 et M3 comme ci-dessus. Un changement dans un sens ou dans l'autre est un signal de révision.

## Alternatives considérées

- **`CODEX_HOME` privé et éphémère par appel**, avec un `auth.json` écrit par la passerelle, sur le modèle du store agy de BUG-157. Le gain est nul tant qu'aucune clé n'est dans l'environnement. Il coûte une copie par appel, et la perte d'un rafraîchissement du jeton ChatGPT fait dans la copie. Option gardée si le cas d'une clé dans l'environnement devient réel.
- **Retirer les clés de l'environnement des commandes** (`-c shell_environment_policy…`). C'est moins cher que l'option précédente, mais ce n'est pas prouvable sans inférence réelle. Le comportement par défaut de codex n'est pas établi. Reporté au même cas.
- **Une cage bwrap de la passerelle autour de codex, avec `--disable-userns`.** Elle refuserait l'espace utilisateur que crée le bwrap de codex : son bac à sable ne démarrerait plus. Écartée.
- **Refuser `unshare` dans le seccomp du conteneur.** Cela casse le bac à sable de codex et la cage agy, qui en ont besoin. Écartée.
- **Une cage OS pour claude et gemini dès maintenant**, comme celle d'agy. Sans outil, le modèle n'a rien à exécuter : la cage ne bloquerait aucun geste qu'il peut faire. Le réseau du run web est traité par PLU-162. Écartée jusqu'au harness.
