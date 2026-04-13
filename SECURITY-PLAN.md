# Plan Securite pre-GitHub

**Date :** 2026-04-06
**Statut :** A executer AVANT toute publication du repo

## Probleme

L'historique git contient des credentials en clair dans des fichiers commites.
Tant que le repo reste LOCAL, le risque est faible. Mais avant publication GitHub :

## Fichiers a purger de l'historique git

| Fichier | Contenu sensible |
|---------|-----------------|
| `docs/deployment-guide.html` | Mot de passe VPS, PostgreSQL, token HF, IPs |
| `docs/SSH-IAMINE-GUIDE.md` + `.pdf` | Config SSH complete, fingerprints |
| `docs/infrastructure-ssh.md` + `.pdf` | Topologie reseau, IPs, usernames |
| `docs/reprise-activite-vps.md` + `.pdf` | Procedures acces VPS avec mots de passe |
| `reports/claude/2026-04-05-acces-ssh-complet.md` | Matrice complete des cles SSH |
| `settings-jobs.json` | Config agents avec permissions |

## Credentials a rotater

| Secret | Localisation actuelle | Action |
|--------|----------------------|--------|
| Mot de passe root VPS | deployment-guide.html | Changer sur Contabo |
| Mot de passe PostgreSQL | deployment-guide.html, .env | ALTER USER + .env |
| Token HuggingFace | deployment-guide.html | Revoquer sur hf.co, regenerer |
| Server secret HMAC | .server_secret | Regenerer (invalide tous les tokens API) |
| Cles SSH | docs/*.md | Regenerer id_iamine, claude-master-key |

## Commande de nettoyage git

```bash
# ATTENTION : reecrit l'historique — a faire sur une copie
# Installer d'abord : pip install git-filter-repo

git filter-repo \
  --path docs/deployment-guide.html \
  --path docs/SSH-IAMINE-GUIDE.md \
  --path docs/SSH-IAMINE-GUIDE.pdf \
  --path docs/infrastructure-ssh.md \
  --path docs/infrastructure-ssh.pdf \
  --path docs/reprise-activite-vps.md \
  --path docs/reprise-activite-vps.pdf \
  --path reports/ \
  --path settings-jobs.json \
  --path CLAUDE-REGIS.md \
  --path CLAUDE-WASA.md \
  --path CLAUDE-DAVID.md \
  --path CLAUDE-TESTER.md \
  --path wasa/ \
  --invert-paths
```

## Code a nettoyer

| Fichier | Probleme | Action |
|---------|----------|--------|
| `config.py` | CPU_SCORES + GPU_SCORES (100+ lignes de code mort) | Supprimer |
| `settings.py` | Default DB password dans le code | Mettre `""` comme default |
| `pool.py` | Aucun secret hardcode (OK) | — |

## .gitignore mis a jour

Ajouts du 2026-04-06 :
- `docs/deployment-guide.html` et tous les guides SSH
- `settings-jobs.json`
- `=*` (artefacts pip)

## Checklist avant GitHub public

- [ ] git filter-repo execute (historique propre)
- [ ] Rotation de TOUS les credentials listes ci-dessus
- [ ] Verification : `git log --all -p | grep -i password` retourne 0 resultat
- [ ] .gitignore couvre tous les fichiers sensibles
- [ ] README.md a jour
- [ ] LICENSE choisie (MIT ? Apache 2.0 ?)
- [ ] CONTRIBUTING.md
- [ ] Repo prive d'abord, public apres validation David

---

*Maintenu par l'Agent Securite — verifie avant chaque push.*
