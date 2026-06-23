# Réservation du nom `iamine-ai` sur le PyPI public (anti dependency-confusion)

> Action **sortante** à exécuter par David (credentials pypi.org requis).
> L'agent ne publie pas — il prépare seulement le paquet et la procédure.

## Le risque (audit 2026-06-22, item #1)

Les scripts d'install/auto-update font :

```
pip install iamine-ai -i https://cellule.ai/pypi --extra-index-url https://pypi.org/simple
```

Quand pip voit deux index, il prend **la version la plus haute toutes sources
confondues**, sans préférer l'index privé. Or `iamine-ai` **n'est pas réservé**
sur le PyPI public (https://pypi.org/pypi/iamine-ai/json → 404). N'importe qui
peut donc publier un `iamine-ai==99.0.0` piégé sur pypi.org : pip le préférerait
à notre `1.0.3` privé → **exécution de code arbitraire** sur workers / VPS / CI.

La signature Ed25519 des releases ne protège pas ce vecteur : elle est vérifiée
**au boot du pool** (warning-only par défaut), pas pendant le `pip install`.

## Le correctif décisif : posséder le nom

Réserver `iamine-ai` sur le PyPI public sous **notre** compte → plus personne ne
peut publier sous ce nom. La version du placeholder est `0.0.1` (< 1.0.3), donc
**pip continue de prendre la 1.0.3 du PyPI privé** : la commande d'install
publique **ne change pas** (doctrine « simple à participer » respectée).

### Étapes (David)

```bash
cd tools/pypi-name-reservation
python -m build                      # produit dist/iamine_ai-0.0.1*
python -m twine upload dist/*        # demande un compte + token API pypi.org
```

Vérifier : https://pypi.org/project/iamine-ai/ existe et appartient à notre compte.

> ⚠️ Ne JAMAIS bumper ce placeholder au-dessus de la vraie version. Il doit
> rester en `0.0.x` pour que l'index privé gagne toujours.

## Défense en profondeur (optionnel, côté build/CI uniquement)

À ne PAS mettre dans la commande d'install publique (friction). Pour les chemins
internes (`scripts/auto-update.sh`, CI), on peut épingler la version exacte :

```
pip install "iamine-ai==1.0.3" -i https://cellule.ai/pypi --extra-index-url https://pypi.org/simple
```

La réservation du nom reste la mesure principale ; le pin n'est qu'un complément.
