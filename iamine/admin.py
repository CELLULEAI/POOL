"""Dashboard Admin IAMINE — /admin/models"""

from __future__ import annotations

import json
import time


def render_admin_dashboard(pool, models_registry: list, active_family: str = "qwen3.5", available_families: list[str] | None = None) -> str:
    """Genere la page HTML du dashboard admin."""
    workers = pool.workers
    uptime = int(time.time() - getattr(pool, '_start_time', time.time()))
    up_h, up_m = divmod(uptime // 60, 60)

    # Collecter les donnees workers
    rows = []
    total_tps = 0
    qwen35_count = 0
    qwen25_count = 0

    for wid, w in workers.items():
        model = w.info.get("model_path", "?").split("/")[-1].replace(".gguf", "")
        real_tps = w.info.get("real_tps", 0) or 0
        bench_tps = w.info.get("bench_tps", 0) or 0
        eff_tps = real_tps if real_tps > 0 else bench_tps
        total_tps += eff_tps
        jobs_ok = w.info.get("total_jobs", 0) or w.jobs_done
        jobs_fail = w.info.get("jobs_failed", 0) or 0
        gpu = w.info.get("gpu", "") or ""
        ram = w.info.get("ram_total_gb", 0)
        version = w.info.get("version", "?")
        platform = w.info.get("platform", "")

        # Compter les workers sur la famille active
        is_active_family = any(m.hf_file.replace(".gguf", "") in model for m in models_registry)
        if is_active_family:
            qwen35_count += 1  # reuse var name for "on-family count"

        # Status color
        if not is_active_family:
            color = "#f0883e"  # orange = modele hors famille active
        elif real_tps > 0 and real_tps < 1.0:
            color = "#f85149"  # rouge = trop lent
        elif jobs_fail > 0 and jobs_ok > 0 and jobs_fail / max(jobs_ok, 1) > 0.1:
            color = "#f85149"
        else:
            color = "#3fb950"  # vert

        rows.append({
            "id": wid, "model": model, "bench_tps": bench_tps,
            "real_tps": real_tps, "eff_tps": eff_tps,
            "jobs_ok": jobs_ok, "jobs_fail": jobs_fail,
            "gpu": gpu[:25], "ram": ram, "version": version,
            "color": color, "busy": w.busy, "platform": platform,
            "has_gpu": w.info.get("has_gpu", False),
        })

    rows.sort(key=lambda r: r["eff_tps"], reverse=True)

    # Tableau workers HTML
    worker_rows = ""
    for r in rows:
        status = "BUSY" if r["busy"] else "idle"
        tps_display = f'{r["real_tps"]:.1f}' if r["real_tps"] > 0 else "-"
        # Dropdown modeles compatibles (filtres par RAM du worker)
        model_options = ""
        for m in models_registry:
            if m.ram_required_gb <= max(r["ram"], 1):
                selected = " style='color:#3fb950'" if m.hf_file.replace(".gguf", "") in r["model"] else ""
                model_options += f'<option value="{m.id}"{selected}>{m.name} ({m.size_gb}G)</option>'
        worker_rows += f"""<tr style="border-bottom:1px solid #333;">
<td style="color:{r['color']};font-weight:bold">{r['id']}</td>
<td>{status}</td>
<td>{r['model'][:35]}</td>
<td>{r['bench_tps']:.1f}</td>
<td style="font-weight:bold">{tps_display}</td>
<td>{r['jobs_ok']}</td>
<td style="color:{'#f85149' if r['jobs_fail']>5 else '#8b949e'}">{r['jobs_fail']}</td>
<td>{r['gpu']}</td>
<td>{r['ram']:.0f}G</td>
<td>{r['version']}</td>
<td>
<select onchange="assignModel('{r['id']}',this.value);this.selectedIndex=0" style="background:#21262d;color:#e0e0e0;border:1px solid #444;padding:2px;font-size:11px">
<option value="">Modele...</option>
{model_options}
</select>
<select onchange="workerAction('{r['id']}',this.value);this.selectedIndex=0" style="background:#21262d;color:#e0e0e0;border:1px solid #444;padding:2px;font-size:11px;margin-left:2px">
<option value="">Cmd...</option>
<option value="self_update">Self-update</option>
<option value="restart">Restart</option>
</select>
</td></tr>
"""

    # Modeles dispo
    model_rows = ""
    for m in models_registry:
        mtype = "MoE" if m.model_type == "moe" else "Dense"
        active_workers = sum(1 for r in rows if m.hf_file.replace(".gguf", "") in r["model"])
        model_rows += f"""<tr style="border-bottom:1px solid #333;">
<td>{m.name}</td><td>{m.params}</td><td>{mtype}</td>
<td>{m.size_gb}G</td><td>{m.ram_required_gb}G</td>
<td>{m.quality_score}</td><td>{active_workers}</td>
</tr>"""

    # Stats graphique ASCII
    bars = ""
    max_tps = max((r["eff_tps"] for r in rows), default=1) or 1
    for r in rows[:15]:
        width = int(r["eff_tps"] / max_tps * 200)
        bars += f'<div style="margin:2px 0"><span style="display:inline-block;width:140px;font-size:11px">{r["id"][:16]}</span><span style="display:inline-block;width:{width}px;height:14px;background:{r["color"]};border-radius:2px"></span> <span style="font-size:11px;color:#8b949e">{r["eff_tps"]:.1f} t/s</span></div>'

    pct35 = qwen35_count * 100 // max(len(rows), 1)

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>IAMINE Admin</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#1a1a2e;color:#e0e0e0;font-family:'Courier New',monospace;padding:20px}}
h1{{color:#58a6ff;margin-bottom:5px}}
h2{{color:#58a6ff;margin:20px 0 10px;font-size:16px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;padding:6px;color:#8b949e;border-bottom:2px solid #444}}
td{{padding:5px 6px}}
tr:hover{{background:#21262d}}
.btn{{background:#238636;color:white;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;font-size:12px;margin:2px}}
.btn:hover{{background:#2ea043}}
.btn-warn{{background:#da3633}}
.btn-warn:hover{{background:#f85149}}
.stat{{display:inline-block;background:#21262d;padding:10px 20px;border-radius:6px;margin:5px;text-align:center}}
.stat b{{display:block;font-size:24px;color:#58a6ff}}
.stat span{{font-size:11px;color:#8b949e}}
</style></head><body>
<h1>IAMINE Pool Admin</h1>
<div style="color:#8b949e;margin-bottom:15px">v{getattr(pool, '_version', '0.2.4')} | uptime {up_h}h{up_m:02d}m</div>

<div>
<div class="stat"><b>{len(rows)}</b><span>Workers</span></div>
<div class="stat"><b>{total_tps:.0f}</b><span>Total t/s</span></div>
<div class="stat"><b>{pct35}%</b><span>{active_family.upper()}</span></div>
<div class="stat"><b>{sum(r['jobs_ok'] for r in rows)}</b><span>Jobs OK</span></div>
</div>

<div style="margin:15px 0">
<label style="color:#8b949e;font-size:12px;margin-right:5px">Famille LLM :</label>
<select id="family-select" onchange="switchFamily(this.value)" style="background:#238636;color:white;border:none;padding:8px 12px;border-radius:4px;font-size:12px;cursor:pointer">
{''.join(f'<option value="{f}"{"selected" if f == active_family else ""}>{f.upper()}</option>' for f in (available_families or [active_family]))}
</select>
<button class="btn" onclick="location.reload()" style="margin-left:10px">Rafraichir</button>
<button class="btn" onclick="migrateAll()">Migrer tout {active_family.upper()}</button>
</div>

<h2>Workers</h2>
<table>
<tr><th>Worker</th><th>Status</th><th>Modele</th><th>Bench</th><th>Real t/s</th><th>Jobs</th><th>Fail</th><th>GPU</th><th>RAM</th><th>Ver</th><th>Action</th></tr>
{worker_rows}
</table>

<h2>Performance par worker</h2>
{bars}

<h2>Modeles disponibles</h2>
<table>
<tr><th>Modele</th><th>Params</th><th>Type</th><th>GGUF</th><th>RAM min</th><th>Qualite</th><th>Workers</th></tr>
{model_rows}
</table>

<script>
function workerAction(wid, cmd) {{
    if (!cmd) return;
    fetch('/admin/api/worker-cmd', {{
        method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{worker_id:wid, cmd:cmd}})
    }}).then(r=>r.json()).then(d=>{{alert(JSON.stringify(d));location.reload()}});
}}
function assignModel(wid, modelId) {{
    if (!modelId) return;
    if (!confirm('Assigner ' + modelId + ' a ' + wid + ' ?')) return;
    fetch('/admin/api/assign', {{
        method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{worker_id:wid, model_id:modelId}})
    }}).then(r=>r.json()).then(d=>{{
        if(d.ok) alert('OK: ' + wid + ' → ' + d.model);
        else alert('Erreur: ' + (d.error||JSON.stringify(d)));
        location.reload();
    }});
}}
function migrateAll() {{
    if(!confirm('Migrer tous les workers vers la famille active ?')) return;
    fetch('/admin/api/migrate-all',{{method:'POST'}}).then(r=>r.json()).then(d=>{{
        alert('Migres: '+d.migrated+'\\n'+JSON.stringify(d.details,null,2));
        location.reload();
    }});
}}
function switchFamily(family) {{
    if(!confirm('Changer la famille LLM du pool vers ' + family.toUpperCase() + ' ?\\n\\nTous les workers vont etre migres automatiquement.')) {{
        document.getElementById('family-select').value = '{active_family}';
        return;
    }}
    fetch('/admin/api/set-family', {{
        method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{family: family}})
    }}).then(r=>r.json()).then(d=>{{
        if(d.ok) alert('Famille: ' + d.new_family + '\\nWorkers migres: ' + d.migrated);
        else alert('Erreur: ' + (d.error||JSON.stringify(d)));
        location.reload();
    }});
}}
setTimeout(()=>location.reload(), 30000);
</script>
</body></html>"""
