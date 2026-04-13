"""Pipeline distribué v2 — VRAM partagée via PostgreSQL.

Chaque worker LLM est un "neurone" isolé (pas de VRAM partagée).
On compense par une table workspace PostgreSQL qui sert de mémoire
partagée structurée entre les étapes du pipeline :

  DRAFT   → lit workspace.topic/style   → écrit workspace.draft
  ENRICH  → lit workspace.draft/facts   → écrit workspace.enriched
  VERIFY  → lit workspace.enriched/facts → écrit workspace.issues
  SUMMARIZE → lit workspace.enriched     → écrit workspace.facts/summary

La table workspace garantit que le topic, le style et les faits
accumulés ne sont JAMAIS perdus entre étapes, même si un petit
modèle 1.5B-3B ne comprend pas bien le prompt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("iamine.pipeline")


class Role(str, Enum):
    DRAFT = "draft"
    ENRICH = "enrich"
    VERIFY = "verify"
    SUMMARIZE = "summarize"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class PipelineTask:
    task_id: str
    pipeline_id: str
    step: int
    role: Role
    prompt: str
    context: str = ""
    result: str = ""
    status: TaskStatus = TaskStatus.PENDING
    worker_id: str = ""
    tokens: int = 0
    duration_sec: float = 0
    created: float = field(default_factory=time.time)


class Pipeline:
    """Orchestre un pipeline de tâches distribuées via PostgreSQL workspace."""

    ROLE_PREFERENCE = {
        Role.DRAFT: ["1.5B", "3B", "0.5B"],
        Role.ENRICH: ["7B", "14B", "3B"],
        Role.VERIFY: ["3B", "7B", "1.5B"],
        Role.SUMMARIZE: ["7B", "14B", "3B"],
    }

    # Tokens max par role — petit ctx = inference rapide, la DB compense
    # Un 1.5B a 2 tok/s genere 512 tokens en ~4 min (sous le heartbeat 90s pour les GPU)
    ROLE_MAX_TOKENS = {
        Role.DRAFT: 512,      # ebauche — generer le contenu brut
        Role.ENRICH: 512,     # amelioration — rester concis, la qualite pas la longueur
        Role.VERIFY: 256,     # verification — juste lister les erreurs trouvees
        Role.SUMMARIZE: 256,  # extraction de faits — liste numerotee, 15 faits max
    }

    def __init__(self, pool):
        self.pool = pool
        self._db_ready = False

    # ── DB workspace ──────────────────────────────────────────────

    async def _ensure_workspace_table(self):
        """Crée la table workspace si elle n'existe pas."""
        if self._db_ready:
            return
        if not hasattr(self.pool.store, 'pool') or not self.pool.store.pool:
            return
        try:
            async with self.pool.store.pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS pipeline_workspace (
                        pipeline_id  TEXT NOT NULL,
                        chapter      INTEGER DEFAULT 1,
                        topic        TEXT NOT NULL DEFAULT '',
                        style        TEXT NOT NULL DEFAULT 'literary',
                        instructions TEXT DEFAULT '',
                        draft        TEXT DEFAULT '',
                        enriched     TEXT DEFAULT '',
                        verification TEXT DEFAULT '',
                        issues       JSONB DEFAULT '[]'::jsonb,
                        summary      TEXT DEFAULT '',
                        facts        JSONB DEFAULT '[]'::jsonb,
                        step_current TEXT DEFAULT 'pending',
                        total_tokens INTEGER DEFAULT 0,
                        duration_sec REAL DEFAULT 0,
                        steps_log    JSONB DEFAULT '[]'::jsonb,
                        created      TIMESTAMP DEFAULT NOW(),
                        PRIMARY KEY (pipeline_id, chapter)
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS pipeline_outputs (
                        pipeline_id TEXT PRIMARY KEY,
                        chapter INTEGER,
                        title TEXT,
                        text TEXT,
                        summary TEXT,
                        verification TEXT,
                        duration_sec REAL,
                        total_tokens INTEGER,
                        steps JSONB,
                        created TIMESTAMP DEFAULT NOW()
                    )
                """)
            self._db_ready = True
        except Exception as e:
            log.warning(f"Workspace table creation failed: {e}")

    async def _ws_write(self, pipeline_id: str, chapter: int, **fields):
        """Écrit dans le workspace PostgreSQL."""
        await self._ensure_workspace_table()
        if not hasattr(self.pool.store, 'pool') or not self.pool.store.pool:
            return
        try:
            async with self.pool.store.pool.acquire() as conn:
                # Upsert
                cols = list(fields.keys())
                placeholders = ", ".join(f"${i+3}" for i in range(len(cols)))
                updates = ", ".join(f"{c} = ${i+3}" for i, c in enumerate(cols))
                col_names = ", ".join(cols)
                await conn.execute(f"""
                    INSERT INTO pipeline_workspace (pipeline_id, chapter, {col_names})
                    VALUES ($1, $2, {placeholders})
                    ON CONFLICT (pipeline_id, chapter)
                    DO UPDATE SET {updates}
                """, pipeline_id, chapter, *fields.values())
        except Exception as e:
            log.warning(f"Workspace write failed: {e}")

    async def _ws_read(self, pipeline_id: str, chapter: int) -> dict:
        """Lit le workspace complet pour un chapitre."""
        await self._ensure_workspace_table()
        if not hasattr(self.pool.store, 'pool') or not self.pool.store.pool:
            return {}
        try:
            async with self.pool.store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM pipeline_workspace WHERE pipeline_id=$1 AND chapter=$2",
                    pipeline_id, chapter
                )
                if row:
                    return dict(row)
        except Exception as e:
            log.warning(f"Workspace read failed: {e}")
        return {}

    # ── Worker selection ──────────────────────────────────────────

    def _select_worker_for_role(self, role: Role, exclude: list[str] = None) -> str | None:
        """Sélectionne le meilleur worker disponible pour un rôle."""
        exclude = exclude or []
        preferences = self.ROLE_PREFERENCE.get(role, ["3B"])
        candidates = []

        for w in self.pool.workers.values():
            if w.busy or w.worker_id in exclude:
                continue
            model_path = w.info.get("model_path", "")
            model_size = ""
            for part in model_path.replace("-", ".").split("."):
                if part.endswith("b") and part[:-1].replace(".", "").isdigit():
                    model_size = part.upper()
            try:
                pref_score = len(preferences) - preferences.index(model_size)
            except ValueError:
                pref_score = 0
            if w.info.get("has_gpu"):
                pref_score += 5
            tps = w.info.get("bench_tps") or 1.0
            pref_score += tps / 10.0
            candidates.append((w.worker_id, pref_score))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    # ── Step execution ────────────────────────────────────────────

    async def _execute_step(self, task: PipelineTask, used_workers: list[str] = None) -> str:
        """Exécute une étape du pipeline sur un worker.

        Strategie : essayer avec exclusion, puis sans exclusion, puis submit_job.
        Les etapes sont sequentielles → les workers precedents sont libres.
        """
        used = used_workers or []

        full_prompt = task.prompt
        if task.context:
            full_prompt = f"CONTEXT:\n{task.context}\n\nTASK:\n{task.prompt}"

        messages = [{"role": "user", "content": full_prompt}]

        # Chercher un worker : avec exclusion, puis sans, puis n'importe lequel
        worker = None
        for attempt_exclude in [used, []]:
            target_id = self._select_worker_for_role(task.role, exclude=attempt_exclude)
            if target_id:
                w = self.pool.workers.get(target_id)
                if w and not w.busy:
                    worker = w
                    break

        # Dernier fallback : submit_job (utilise la queue du pool)
        if not worker:
            try:
                log.info(f"Pipeline step {task.step} ({task.role}): no direct worker, using submit_job")
                result = await self.pool.submit_job(messages=messages, max_tokens=self.ROLE_MAX_TOKENS.get(task.role, 512))
                task.result = result.get("text", "")
                task.tokens = result.get("tokens_generated", 0)
                task.worker_id = result.get("worker_id", "")
                if not task.result.strip():
                    log.warning(f"Pipeline step {task.step} ({task.role}) returned empty")
                return task.result
            except Exception as e:
                log.warning(f"Pipeline step {task.step} ({task.role}) failed: {e}")
                return ""

        # Timeout adaptatif : max_tokens / bench_tps * 1.5, minimum 30s
        bench_tps = worker.info.get("bench_tps") or 5.0
        timeout = max(30, int(2048 / bench_tps * 1.5))

        job_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self.pool.pending_jobs[job_id] = type('PJ', (), {
            'job_id': job_id, 'messages': messages, 'max_tokens': 2048, 'future': future
        })()
        worker.busy = True
        try:
            await worker.ws.send_json({
                "type": "job",
                "job_id": job_id,
                "messages": messages,
                "max_tokens": 2048,
            })
            log.info(f"Pipeline step {task.step} ({task.role}) → {worker.worker_id} (timeout={timeout}s)")
            result_data = await asyncio.wait_for(future, timeout=timeout)
            task.result = result_data.get("text", "")
            task.tokens = result_data.get("tokens_generated", 0)
            task.worker_id = worker.worker_id
            if not task.result.strip():
                log.warning(f"Pipeline step {task.step} ({task.role}) returned empty from {worker.worker_id}")
        except asyncio.TimeoutError:
            log.warning(f"Pipeline step {task.step} ({task.role}) timeout on {worker.worker_id} ({timeout}s) — retrying via submit_job")
            # Retry via submit_job (pool routing)
            try:
                result = await self.pool.submit_job(messages=messages, max_tokens=self.ROLE_MAX_TOKENS.get(task.role, 512))
                task.result = result.get("text", "")
                task.tokens = result.get("tokens_generated", 0)
                task.worker_id = result.get("worker_id", "")
            except Exception as e2:
                log.warning(f"Pipeline step {task.step} ({task.role}) retry failed: {e2}")
        except Exception as e:
            log.warning(f"Pipeline step {task.step} ({task.role}) on {worker.worker_id} failed: {e}")
        finally:
            worker.busy = False
            self.pool._worker_freed.set()
            self.pool.pending_jobs.pop(job_id, None)

        return task.result

    # ── Prompt builders (lisant le workspace) ─────────────────────

    def _build_draft_prompt(self, ws: dict) -> tuple[str, str]:
        """Construit le prompt DRAFT depuis le workspace."""
        topic = ws.get("topic", "")
        style = ws.get("style", "literary")
        instructions = ws.get("instructions", "")
        chapter = ws.get("chapter", 1)
        prev_facts = ws.get("facts", [])

        context_parts = []
        if prev_facts:
            context_parts.append("KNOWN FACTS FROM PREVIOUS CHAPTERS:\n" +
                                 "\n".join(f"- {f}" for f in prev_facts))

        context = "\n\n".join(context_parts) if context_parts else ""

        if style == "factual":
            prompt = (
                f"TOPIC: {topic}\n"
                f"CHAPTER: {chapter}\n"
                f"{'INSTRUCTIONS: ' + instructions if instructions else ''}\n\n"
                "Write a FACTUAL, INFORMATIVE chapter about the topic above.\n"
                "Rules:\n"
                "- Write about the TOPIC. Do NOT invent characters or stories.\n"
                "- Use concrete examples, data, and explanations.\n"
                "- NO fiction, NO novels, NO characters, NO dialogue.\n"
                "- Educational and clear. Be concise but substantive.\n"
                "- Write in French."
            )
        else:
            prompt = (
                f"TOPIC: {topic}\n"
                f"CHAPTER: {chapter}\n"
                f"{'INSTRUCTIONS: ' + instructions if instructions else ''}\n\n"
                "Write Chapter {chapter} of a literary work about the topic above.\n"
                "Include dialogue, descriptions, emotions. Be creative.\n"
                "Be creative and concise. Write in French."
            )
        return context, prompt

    def _build_enrich_prompt(self, ws: dict) -> tuple[str, str]:
        """Construit le prompt ENRICH depuis le workspace."""
        draft = ws.get("draft", "")
        topic = ws.get("topic", "")
        style = ws.get("style", "literary")

        context = f"TOPIC: {topic}\nSTYLE: {style}\n\nORIGINAL DRAFT:\n{draft[:1500]}"

        if style == "factual":
            prompt = (
                "Improve this chapter draft about the TOPIC.\n"
                "- Add more concrete examples and explanations\n"
                "- Improve structure with clear sections\n"
                "- Keep ALL facts from the original\n"
                "- Do NOT add fiction, characters, or stories\n"
                "- Output the improved chapter in French. Be concise but complete."
            )
        else:
            prompt = (
                "Rewrite and improve this chapter draft:\n"
                "- Richer vocabulary and metaphors\n"
                "- Deeper character emotions\n"
                "- Better dialogue with subtext\n"
                "- Sensory details (sight, sound, smell)\n"
                "- Keep ALL plot elements and facts\n"
                "Output the improved chapter in French. At least 500 words."
            )
        return context, prompt

    def _build_verify_prompt(self, ws: dict) -> tuple[str, str]:
        """Construit le prompt VERIFY depuis le workspace."""
        enriched = ws.get("enriched", "") or ws.get("draft", "")
        topic = ws.get("topic", "")
        facts = ws.get("facts", [])

        context = f"TOPIC: {topic}\n"
        if facts:
            context += "KNOWN FACTS:\n" + "\n".join(f"- {f}" for f in facts) + "\n"
        context += f"\nCHAPTER TEXT:\n{enriched[:1500]}"

        prompt = (
            "Analyze this chapter as a fact-checker:\n"
            "1. Does the text match the TOPIC? If not, explain what is wrong.\n"
            "2. List any CONTRADICTIONS with known facts\n"
            "3. List any FACTUAL ERRORS\n"
            "4. Rate: GOOD / NEEDS_WORK / OFF_TOPIC\n\n"
            "Be specific. Quote problematic text. If consistent, explain WHY."
        )
        return context, prompt

    def _build_summarize_prompt(self, ws: dict) -> tuple[str, str]:
        """Construit le prompt SUMMARIZE depuis le workspace."""
        enriched = ws.get("enriched", "") or ws.get("draft", "")
        topic = ws.get("topic", "")

        context = f"TOPIC: {topic}\n\nCHAPTER:\n{enriched[:1500]}"
        prompt = (
            "Extract ALL key facts from this chapter as a numbered list:\n"
            "- Key concepts and definitions\n"
            "- Examples mentioned\n"
            "- Names, locations, dates\n"
            "- Important conclusions\n"
            "Be exhaustive. One fact per line. Max 15 facts."
        )
        return context, prompt

    # ── Main pipeline ─────────────────────────────────────────────

    async def generate_chapter(self, chapter_num: int, title: str,
                                context: str, instructions: str,
                                style: str = "literary",
                                topic: str = "",
                                prev_facts: list = None) -> dict:
        """Génère un chapitre via le pipeline 4 étapes avec workspace PostgreSQL."""
        pipeline_id = uuid.uuid4().hex[:8]
        start = time.time()
        effective_topic = topic or title or "Untitled"
        effective_title = title if title != "Untitled" else effective_topic

        log.info(f"Pipeline {pipeline_id}: Chapter {chapter_num} — {effective_topic}")

        # Initialiser le workspace
        await self._ws_write(pipeline_id, chapter_num,
            topic=effective_topic,
            style=style,
            instructions=instructions or "",
            facts=json.dumps(prev_facts or []),
            step_current="draft"
        )

        used = []

        # ── STEP 1: DRAFT ──
        ws = await self._ws_read(pipeline_id, chapter_num)
        if not ws:
            ws = {"topic": effective_topic, "style": style, "instructions": instructions,
                  "chapter": chapter_num, "facts": prev_facts or []}
        ws["chapter"] = chapter_num
        draft_ctx, draft_prompt = self._build_draft_prompt(ws)

        draft_task = PipelineTask(
            task_id=uuid.uuid4().hex[:8], pipeline_id=pipeline_id,
            step=1, role=Role.DRAFT, context=draft_ctx, prompt=draft_prompt,
        )
        draft = await self._execute_step(draft_task, used)
        if not draft:
            return {"error": "Draft failed", "pipeline_id": pipeline_id}
        used.append(draft_task.worker_id)

        await self._ws_write(pipeline_id, chapter_num,
            draft=draft, step_current="enrich")
        log.info(f"Pipeline {pipeline_id}: DRAFT done — {draft_task.tokens} tok by {draft_task.worker_id}")

        # ── STEP 2: ENRICH ──
        ws = await self._ws_read(pipeline_id, chapter_num) or ws
        ws["draft"] = draft
        enrich_ctx, enrich_prompt = self._build_enrich_prompt(ws)

        enrich_task = PipelineTask(
            task_id=uuid.uuid4().hex[:8], pipeline_id=pipeline_id,
            step=2, role=Role.ENRICH, context=enrich_ctx, prompt=enrich_prompt,
        )
        enriched = await self._execute_step(enrich_task, used)
        final_text = enriched if enriched else draft
        used.append(enrich_task.worker_id)

        await self._ws_write(pipeline_id, chapter_num,
            enriched=final_text, step_current="verify")
        log.info(f"Pipeline {pipeline_id}: ENRICH done — {enrich_task.tokens} tok by {enrich_task.worker_id}")

        # ── STEP 3: VERIFY (fallback sans exclusion si pas de worker dispo) ──
        ws = await self._ws_read(pipeline_id, chapter_num) or ws
        ws["enriched"] = final_text
        verify_ctx, verify_prompt = self._build_verify_prompt(ws)

        verify_task = PipelineTask(
            task_id=uuid.uuid4().hex[:8], pipeline_id=pipeline_id,
            step=3, role=Role.VERIFY, context=verify_ctx, prompt=verify_prompt,
        )
        verification = await self._execute_step(verify_task, used)
        if verify_task.worker_id:
            used.append(verify_task.worker_id)

        await self._ws_write(pipeline_id, chapter_num,
            verification=verification, step_current="summarize")
        log.info(f"Pipeline {pipeline_id}: VERIFY done — {verify_task.tokens} tok by {verify_task.worker_id}")

        # ── STEP 4: SUMMARIZE ──
        ws = await self._ws_read(pipeline_id, chapter_num) or ws
        ws["enriched"] = final_text
        sum_ctx, sum_prompt = self._build_summarize_prompt(ws)

        summary_task = PipelineTask(
            task_id=uuid.uuid4().hex[:8], pipeline_id=pipeline_id,
            step=4, role=Role.SUMMARIZE, context=sum_ctx, prompt=sum_prompt,
        )
        summary = await self._execute_step(summary_task, used)

        await self._ws_write(pipeline_id, chapter_num,
            summary=summary, step_current="done")
        log.info(f"Pipeline {pipeline_id}: SUMMARIZE done — {summary_task.tokens} tok by {summary_task.worker_id}")

        duration = time.time() - start
        total_tokens = sum(t.tokens for t in [draft_task, enrich_task, verify_task, summary_task])

        result = {
            "pipeline_id": pipeline_id,
            "chapter": chapter_num,
            "title": effective_title,
            "text": final_text,
            "summary": summary,
            "verification": verification,
            "duration_sec": round(duration, 1),
            "total_tokens": total_tokens,
            "steps": [
                {"role": "draft", "worker": draft_task.worker_id, "tokens": draft_task.tokens},
                {"role": "enrich", "worker": enrich_task.worker_id, "tokens": enrich_task.tokens},
                {"role": "verify", "worker": verify_task.worker_id, "tokens": verify_task.tokens},
                {"role": "summarize", "worker": summary_task.worker_id, "tokens": summary_task.tokens},
            ],
        }

        # Sauvegarder dans pipeline_outputs
        await self._save_to_db(result)
        return result

    async def _save_to_db(self, result: dict):
        """Sauvegarde le résultat final en PostgreSQL."""
        if not hasattr(self.pool.store, 'pool') or not self.pool.store.pool:
            return
        try:
            async with self.pool.store.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO pipeline_outputs
                    (pipeline_id, chapter, title, text, summary, verification, duration_sec, total_tokens, steps)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (pipeline_id) DO UPDATE SET
                    text=$4, summary=$5, verification=$6, duration_sec=$7, total_tokens=$8, steps=$9
                """,
                    result["pipeline_id"], result["chapter"], result["title"],
                    result["text"], result["summary"], result["verification"],
                    result["duration_sec"], result["total_tokens"],
                    json.dumps(result["steps"]),
                )
        except Exception as e:
            log.warning(f"Failed to save pipeline: {e}")

    async def generate_book(self, title: str, chapters: list[dict],
                             base_context: str = "",
                             style: str = "literary",
                             topic: str = "") -> list[dict]:
        """Génère un livre complet. Les faits s'accumulent via le workspace."""
        results = []
        accumulated_facts = []

        for ch in chapters:
            ch_topic = topic or ch.get("instructions", "") or title
            result = await self.generate_chapter(
                chapter_num=ch["num"],
                title=ch.get("title", f"Chapitre {ch['num']}"),
                context=base_context,
                instructions=ch.get("instructions", ""),
                style=style,
                topic=ch_topic,
                prev_facts=accumulated_facts,
            )
            results.append(result)

            # Extraire les faits du résumé pour le chapitre suivant
            if result.get("summary"):
                for line in result["summary"].split("\n"):
                    line = line.strip().lstrip("0123456789.-) ")
                    if line and len(line) > 10:
                        accumulated_facts.append(line)
                # Garder les 30 derniers faits
                accumulated_facts = accumulated_facts[-30:]

            log.info(
                f"Book progress: {ch['num']}/{len(chapters)} — "
                f"{result.get('total_tokens', 0)} tok, {result.get('duration_sec', 0)}s — "
                f"{len(accumulated_facts)} facts accumulated"
            )

        return results
