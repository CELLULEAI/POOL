"""Smart Router — route les requetes vers le meilleur worker selon le contexte.

Criteres de routing (par priorite) :
1. Modele demande : le worker doit avoir ce modele charge
2. Contexte : le worker doit supporter le nombre de tokens accumules
3. Disponibilite : le worker ne doit pas etre busy
4. Performance : preferer le worker le plus rapide (bench_tps)
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("iamine.router")


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse version string en tuple pour comparaison semantique."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


_MODEL_SIZE_RE = re.compile(r'[\-_](\d+(?:\.\d+)?)[Bb][\-_\.]')

def _parse_model_size(model_path: str) -> float:
    """Extrait la taille en milliards depuis un path GGUF (ex: 'Qwen3.5-9B' → 9.0)."""
    m = _MODEL_SIZE_RE.search(model_path)
    return float(m.group(1)) if m else 0

# Estimation grossiere : 1 token ~ 4 caracteres
CHARS_PER_TOKEN = 4


@dataclass
class Conversation:
    """Conversation temporaire — vit uniquement pendant la session active.

    Compactage 3 niveaux pour simuler un contexte infini :
    - L1 (RAM)  : messages bruts recents (4 derniers)
    - L2 (RAM)  : resume LLM des messages compactes (plafonné a ~1500 tok)
    - L3 (PostgreSQL) : messages et resumes archives, chiffres avec le token de compte

    Meta-compaction : quand le resume L2 devient trop gros,
    il est re-resume par le LLM et l'ancien resume est archive en L3.
    Resultat : contexte "infini" sans ralentissement.
    """
    conv_id: str
    messages: list[dict] = field(default_factory=list)
    total_tokens: int = 0
    model_used: str = ""
    worker_id: str = ""
    api_token: str = ""
    created: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    ttl_sec: int = 3600  # 1h sans activite = supprime
    compactions: int = 0  # nombre de compactages effectues
    meta_compactions: int = 0  # nombre de meta-compactages du resume
    _summary: str = ""    # resume des messages compactes
    _l3_summary: str = ""  # resume recupere depuis PostgreSQL (L3)

    # Seuil de meta-compaction : quand le resume depasse ~1500 tokens (~6000 chars)
    SUMMARY_MAX_CHARS: int = 6000

    @property
    def expired(self) -> bool:
        # Conversations authentifiees (acc_*) : jamais expirees
        if self.api_token and self.api_token.startswith("acc_"):
            return False
        return (time.time() - self.last_activity) > self.ttl_sec

    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        self.total_tokens += len(content) // CHARS_PER_TOKEN
        self.last_activity = time.time()

    def get_messages(self) -> list[dict]:
        """Retourne les messages avec le resume en premier si compacte."""
        result = []
        if self._summary:
            result.append({
                "role": "system",
                "content": f"[Conversation summary from earlier messages]\n{self._summary}"
            })
        result.extend(self.messages)
        return result

    def get_context_for_worker(self, worker_ctx: int) -> list[dict]:
        """Retourne un contexte adapte a la capacite du worker.

        - System messages TOUJOURS en premier (sinon erreur llama.cpp)
        - Inclut le resume si compacte
        - Limite les messages pour rester sous 60% du ctx du worker
        """
        max_tokens = int(worker_ctx * 0.6)
        system_msgs = []
        non_system = []

        # Separer system et non-system dans l'historique
        for msg in self.messages:
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                non_system.append(msg)

        # Resume compacte = system aussi
        if self._summary:
            system_msgs.append({
                "role": "system",
                "content": f"[Conversation context]\n{self._summary}"
            })

        # Fusionner tous les system en un seul message (evite system multiples)
        if system_msgs:
            combined_system = "\n\n".join(m.get("content", "") for m in system_msgs)
            result = [{"role": "system", "content": combined_system}]
        else:
            result = []

        # Prendre les messages non-system en partant de la fin
        token_count = sum(len(m.get("content", "")) // CHARS_PER_TOKEN for m in result)
        messages_to_add = []
        for msg in reversed(non_system):
            msg_tokens = len(msg.get("content", "")) // CHARS_PER_TOKEN
            if token_count + msg_tokens > max_tokens:
                break
            messages_to_add.insert(0, msg)
            token_count += msg_tokens

        result.extend(messages_to_add)

        if len(result) <= 1:
            # Fallback : au moins le dernier message user
            if non_system:
                result.extend(non_system[-2:] if len(non_system) >= 2 else non_system[-1:])

        return result

    def needs_compaction(self, worker_ctx: int) -> bool:
        """Verifie si la conversation doit etre compactee.
        Seuil : 75% du contexte du worker — compacte avant de saturer.
        Pour un worker 2K ctx : compacte a ~1536 tokens.
        Le compactage produit un resume de ~500 tokens + 2 derniers messages.
        """
        return self.total_tokens > int(worker_ctx * 0.75) or len(self.messages) > 12

    def compact(self, summary: str) -> list[dict]:
        """Compacte la conversation : remplace l'historique par un resume.

        Garde les 4 derniers messages (2 derniers echanges) pour la coherence.
        Tout le reste est purge et remplace par le resume.

        Retourne les messages purgés (pour archivage L3).
        """
        keep_last = 4  # garder les 2 derniers echanges (user+assistant)

        if len(self.messages) <= keep_last:
            return []  # rien a compacter

        # Messages qui vont être archivés en L3
        archived = self.messages[:-keep_last]

        # Sauvegarder les derniers messages
        recent = self.messages[-keep_last:]

        # Purger l'historique complet
        self.messages = recent

        # Mettre a jour le resume (accumule si deja compacte)
        if self._summary:
            self._summary = f"{self._summary}\n\n[Continued]\n{summary}"
        else:
            self._summary = summary

        # Recalculer les tokens
        summary_tokens = len(self._summary) // CHARS_PER_TOKEN
        recent_tokens = sum(len(m.get("content", "")) // CHARS_PER_TOKEN for m in recent)
        self.total_tokens = summary_tokens + recent_tokens

        self.compactions += 1
        self._compacted = True  # flag pour la reponse API
        log.info(
            f"Compacted conv={self.conv_id} — "
            f"compaction #{self.compactions}, "
            f"tokens now: {self.total_tokens}, "
            f"summary: {len(self._summary)} chars, "
            f"archived: {len(archived)} messages → L3"
        )

        return archived

    def needs_meta_compaction(self) -> bool:
        """Le resume L2 est-il trop gros ? Si oui, il faut le re-résumer."""
        return len(self._summary) > self.SUMMARY_MAX_CHARS

    def meta_compact(self, condensed_summary: str) -> str:
        """Meta-compaction : remplace le resume par une version condensee.

        Retourne l'ancien resume (pour archivage en L3).
        """
        old_summary = self._summary
        self._summary = condensed_summary

        # Recalculer les tokens
        summary_tokens = len(self._summary) // CHARS_PER_TOKEN
        recent_tokens = sum(len(m.get("content", "")) // CHARS_PER_TOKEN for m in self.messages)
        self.total_tokens = summary_tokens + recent_tokens

        self.meta_compactions += 1
        log.info(
            f"Meta-compacted conv={self.conv_id} — "
            f"meta #{self.meta_compactions}, "
            f"summary: {len(old_summary)} → {len(self._summary)} chars"
        )

        return old_summary


class SmartRouter:
    """Gestionnaire de conversations + routing intelligent."""

    def __init__(self):
        self._conversations: dict[str, Conversation] = {}
        self._cleanup_counter = 0
        self._round_robin_idx = 0  # Pour distribuer equitablement les nouvelles convs

    def get_or_create_conversation(self, conv_id: str | None = None, api_token: str = "") -> Conversation:
        """Recupere ou cree une conversation."""
        # Nettoyage periodique (toutes les 100 requetes)
        self._cleanup_counter += 1
        if self._cleanup_counter % 100 == 0:
            self._cleanup_expired()

        if conv_id and conv_id in self._conversations:
            conv = self._conversations[conv_id]
            # Isolation : si le token est different, ne pas reutiliser (anti-contamination)
            if conv.api_token and api_token and conv.api_token != api_token:
                log.warning(f"Conv {conv_id} belongs to different token — creating new")
                new_id = uuid.uuid4().hex[:16]
                new_conv = Conversation(conv_id=new_id, api_token=api_token)
                self._conversations[new_id] = new_conv
                return new_conv
            if not conv.expired:
                conv.last_activity = time.time()
                return conv
            else:
                del self._conversations[conv_id]

        # Nouvelle conversation
        new_id = conv_id or uuid.uuid4().hex[:16]
        conv = Conversation(conv_id=new_id, api_token=api_token)
        self._conversations[new_id] = conv
        return conv

    def delete_conversation(self, conv_id: str):
        """Supprime une conversation (bouton Clear)."""
        self._conversations.pop(conv_id, None)
        log.info(f"Conversation {conv_id} supprimee")

    def _is_worker_excluded(self, w, pool_version=None, approved_files=None, exclude_local_hostname=None) -> bool:
        """Quick check si un worker est exclu du routing (pour calcul total_bench)."""
        if w.busy:
            return True
        if exclude_local_hostname and w.info.get("hostname", "") == exclude_local_hostname:
            return True
        if pool_version and not w.info.get("proxy_mode"):
            wv = _parse_version(w.info.get("version", "0.0.0"))
            pv = _parse_version(pool_version)
            if len(wv) >= 3 and len(pv) >= 3 and wv[:2] == pv[:2]:
                if pv[2] - wv[2] > 1:
                    return True
            elif wv < pv:
                return True
        if approved_files and not w.info.get("proxy_mode"):
            mp = w.info.get("model_path", "")
            if not any(f in mp for f in approved_files):
                return True
        return False

    def select_worker(self, conv: Conversation, workers: dict, requested_model: str | None = None, exclude_local_hostname: str | None = None, pool_version: str | None = None, approved_files: set[str] | None = None) -> str | None:
        """Selectionne le meilleur worker pour cette conversation.

        Retourne le worker_id ou None si aucun disponible.

        Logique :
        1. Filtre par modele si demande
        2. Filtre par capacite de contexte (ctx_size >= tokens accumules)
        3. Filtre par disponibilite (pas busy)
        4. Exclut le worker local si un worker externe 3B+ est dispo
        5. Prefere le worker deja assigne a cette conversation (affinite)
        6. Sinon prend le plus rapide (bench_tps)
        """
        candidates = []

        # Workers exclus du routing normal (admin ou tool-only)
        TOOL_ONLY = set()  # All workers handle tool-calls now (Qwen3 native support)
        # Coder-z2 = modele Qwen3-Coder specialise code/JSON. Exclu du routing
        # auto (chat general) car il repond en JSON structure meme pour du chat
        # libre. Accessible uniquement si requested_model contient 'coder'/'code'.
        CODE_ONLY = set()  # All workers handle code now
        ROUTING_EXCLUDED = set()  # RED-z2 actif dans le pool (sans RED.md)

        for w in workers.values():
            if w.busy:
                continue

            if w.worker_id in ROUTING_EXCLUDED:
                continue
            # Scout-z2 reserve aux tool-calls — skip si pas de requested_model match
            if w.worker_id in TOOL_ONLY and requested_model != w.worker_id:
                continue

            # Coder-z2 reserve au code/JSON — skip si requested_model ne mentionne pas coder/code
            if w.worker_id in CODE_ONLY:
                rm_low = (requested_model or '').lower()
                if 'coder' not in rm_low and 'code' not in rm_low:
                    continue

            # Exclure les workers obsoletes (version < pool n-1)
            if pool_version and not w.info.get("proxy_mode"):
                wv = _parse_version(w.info.get("version", "0.0.0"))
                pv = _parse_version(pool_version)
                if len(wv) >= 3 and len(pv) >= 3 and wv[:2] == pv[:2]:
                    if pv[2] - wv[2] > 5:  # tolerance n-5 (workers lents a upgrader)
                        continue
                elif wv < pv:
                    continue

            # Exclure les modeles hors registre (sauf proxy workers)
            if approved_files and not w.info.get("proxy_mode"):
                mp = w.info.get("model_path", "")
                if not any(f in mp for f in approved_files):
                    continue

            # Exclure le worker local (VPS) si demandé
            if exclude_local_hostname and w.info.get("hostname", "") == exclude_local_hostname:
                continue

            # Verifier le contexte — migration au lieu de skip
            worker_ctx = w.info.get("ctx_size", 2048)
            if conv.total_tokens > worker_ctx * 0.95:
                # Hard limit: context > 95% — cannot fit, skip
                continue
            elif conv.total_tokens > worker_ctx * 0.8:
                # Soft limit: 80-95% — penalize heavily, prefer larger ctx workers
                score -= 500  # will be outscored by workers with more room

            # Verifier le modele si demande
            if requested_model:
                worker_model = w.info.get("model_path", "")
                if requested_model != "auto" and requested_model.lower() not in worker_model.lower() and requested_model.lower() not in w.worker_id.lower():
                    continue

            # Score du worker — routing cooperatif pondere
            score = 0.0
            has_gpu = w.info.get("has_gpu", False)
            # Utiliser real_tps si > 0, sinon bench_tps (workers sans job servi)
            _real = w.info.get("real_tps") or 0
            _bench = w.info.get("bench_tps") or 0
            effective_tps = _real if _real > 0 else _bench
            jobs_failed = w.info.get("jobs_failed", 0)

            # Exclure les workers sans benchmark ou trop lents (< 8 t/s)
            # Seuil 8 t/s = minimum pour une inference agreable
            bench = w.info.get("bench_tps") or 0
            if (not effective_tps and not bench) or (effective_tps > 0 and effective_tps < 8.0 and not has_gpu):
                continue

            # Penaliser les gros modeles trop lents sur CPU
            model_path = w.info.get("model_path", "")
            model_size = _parse_model_size(model_path)
            min_tps = {0.8: 5.0, 2: 5.0, 4: 6.0, 9: 7.0, 27: 5.0, 35: 5.0}.get(model_size, 5.0)
            if effective_tps < min_tps and not has_gpu:
                score -= 100

            # Penalite echecs
            if jobs_failed > 10:
                score -= 50
            elif jobs_failed > 5:
                score -= 20

            # === DEFICIT SCORING (routing cooperatif) ===
            # Part attendue = bench_tps / sum(bench_tps de tous les routables)
            # Deficit = part attendue - part reelle → plus le deficit est grand, plus prioritaire
            total_bench = sum(
                (cw.info.get("real_tps") or cw.info.get("bench_tps") or 1)
                for cw in workers.values() if not self._is_worker_excluded(cw, pool_version, approved_files, exclude_local_hostname)
            )
            expected_share = effective_tps / total_bench if total_bench > 0 else 0
            worker_jobs = w.info.get("total_jobs") or 0
            total_jobs = sum((cw.info.get("total_jobs") or 0) for cw in workers.values()) or 1
            actual_share = worker_jobs / total_jobs
            deficit = expected_share - actual_share
            score += deficit * 500  # le levier principal

            # Affinite forte : si le worker a deja servi cette conv ET est idle
            # +300 garantit que la conv reste sur le meme worker (coherence)
            # Si busy → pas de bonus, le deficit choisit un autre (pas de 503)
            if conv.worker_id == w.worker_id and not w.busy:
                score += 300

            # Bonus qualite pour les nouvelles conversations
            if conv.total_tokens < 100:
                score += model_size * 5  # 27B = +135, 0.8B = +4
            elif conv.total_tokens > 2000:
                # Context migration: strongly prefer workers with more headroom
                headroom = worker_ctx - conv.total_tokens
                score += headroom / 100.0  # 100K headroom = +1000, 5K = +50

            candidates.append((w.worker_id, score, worker_ctx))

        if not candidates:
            return None

        # Trier par score decroissant
        candidates.sort(key=lambda x: x[1], reverse=True)

        # Round-robin avec tolérance — regroupe les workers à scores proches
        SCORE_EPSILON = 2.0
        top_score = candidates[0][1]
        tied = [c for c in candidates if abs(c[1] - top_score) < SCORE_EPSILON]
        if len(tied) > 1:
            self._round_robin_idx = (self._round_robin_idx + 1) % len(tied)
            best_worker_id = tied[self._round_robin_idx][0]
        else:
            best_worker_id = candidates[0][0]

        # Mettre a jour l'affinite
        conv.worker_id = best_worker_id

        log.debug(
            f"Route conv={conv.conv_id} ({conv.total_tokens} tok) "
            f"→ {best_worker_id} (score={candidates[0][1]:.1f}, ctx={candidates[0][2]})"
        )

        return best_worker_id

    def check_and_compact(self, conv: Conversation, worker_ctx: int) -> str | None:
        """Verifie si une conversation doit etre compactee.

        Retourne le prompt de resume a envoyer au worker, ou None.
        Le pool enverra ce prompt au worker, recevra le resume,
        puis appellera conv.compact(summary).
        """
        if not conv.needs_compaction(worker_ctx):
            return None

        # Construire le prompt de resume
        to_summarize = conv.messages[:-4] if len(conv.messages) > 4 else conv.messages
        if not to_summarize:
            return None

        # Extraire UNIQUEMENT le contenu user (les reponses assistant sont du bruit)
        facts_raw = []
        for m in to_summarize:
            if m.get("role") != "user":
                continue
            raw = m.get("content", "")
            # Nettoyer : retirer padding repetitif, prefixes "Record:"
            clean = raw.replace("Record:", "").replace("Remember.", "").replace("Remember this.", "").strip()
            # Couper le padding : garder avant la premiere repetition
            sentences = clean.split(". ")
            seen = set()
            unique = []
            for s in sentences:
                key = s.strip().lower()[:40]  # comparer les 40 premiers chars
                if key and key not in seen:
                    unique.append(s.strip())
                    seen.add(key)
            clean = ". ".join(unique)
            if len(clean) > 300:
                clean = clean[:300]
            if clean and len(clean) > 3:
                facts_raw.append(clean)

        if not facts_raw:
            return None

        # Formater comme une liste a resumer
        numbered = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts_raw))

        # Inclure le résumé existant (RAM ou L3 PostgreSQL) pour ne pas perdre les faits
        previous = ""
        if conv._summary:
            previous = f"PREVIOUS SUMMARY (must be preserved):\n{conv._summary}\n\n"
        elif conv._l3_summary:
            # Récupéré depuis PostgreSQL au chargement de la conversation
            previous = f"PREVIOUS SUMMARY (from archive):\n{conv._l3_summary}\n\n"

        prompt = (
            f"{previous}"
            "Below are NEW facts from the conversation.\n"
            "MERGE all facts (previous + new) into ONE numbered list.\n"
            "Rules:\n"
            "- Copy EVERY fact from the previous summary FIRST, then add new ones\n"
            "- PRESERVE ALL proper names, numbers, speeds (t/s), dates, and specific values EXACTLY as stated\n"
            "- If a value was updated, keep the LATEST value\n"
            "- NEVER invent or approximate values — copy them exactly\n"
            "- NEVER add information not present in the facts below\n"
            "- NEVER skip or merge facts — one fact per line\n"
            "- Output ONLY the numbered list, nothing else\n"
            "- Maximum 20 lines\n\n"
            f"NEW FACTS:\n{numbered}"
        )

        log.info(
            f"Compaction needed for conv={conv.conv_id} "
            f"({conv.total_tokens} tok / {worker_ctx} ctx) — "
            f"summarizing {len(to_summarize)} messages"
        )

        return prompt

    def check_and_meta_compact(self, conv: Conversation) -> str | None:
        """Verifie si le resume doit etre meta-compacte.

        Retourne le prompt de condensation a envoyer au worker, ou None.
        """
        if not conv.needs_meta_compaction():
            return None

        prompt = (
            "Merge and deduplicate the facts below into a SHORTER numbered list.\n"
            "Rules:\n"
            "- Keep EVERY unique fact with EXACT values (names, numbers, dates)\n"
            "- Remove ONLY true duplicates (same fact repeated)\n"
            "- One fact per line, numbered\n"
            "- NEVER drop a fact just to make the list shorter\n"
            "- Maximum 15 lines\n\n"
            f"{conv._summary}"
        )

        log.info(
            f"Meta-compaction needed for conv={conv.conv_id} — "
            f"summary: {len(conv._summary)} chars > {conv.SUMMARY_MAX_CHARS} limit"
        )

        return prompt

    def get_stats(self) -> dict:
        """Stats des conversations actives."""
        active = [c for c in self._conversations.values() if not c.expired]
        return {
            "active_conversations": len(active),
            "total_tokens_in_memory": sum(c.total_tokens for c in active),
            "avg_tokens_per_conv": round(
                sum(c.total_tokens for c in active) / len(active) if active else 0
            ),
            "total_compactions": sum(c.compactions for c in active),
            "total_meta_compactions": sum(c.meta_compactions for c in active),
        }

    def drain_expired(self) -> list[str]:
        """Supprime les conversations expirées et retourne leurs IDs pour nettoyage L3."""
        expired = [cid for cid, c in self._conversations.items() if c.expired]
        for cid in expired:
            del self._conversations[cid]
        if expired:
            log.info(f"Cleanup: {len(expired)} conversations expirees supprimees (L1+L2)")
        return expired

    def _cleanup_expired(self):
        """Supprime les conversations expirees — zero trace."""
        self.drain_expired()
