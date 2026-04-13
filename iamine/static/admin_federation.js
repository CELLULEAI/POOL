// M8 — Admin dashboard fédération. Vanilla JS, pas de framework.
// Se branche sur #tab-federation dans admin.html.

(function () {
  'use strict';

  const FED = {
    state: { self: null, peers: [], heartbeat: null, ledger: [] },
  };

  function $(sel, root = document) {
    return root.querySelector(sel);
  }

  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') el.className = v;
      else if (k === 'style') el.style.cssText = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c == null) continue;
      el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return el;
  }

  function adminToken() {
    // admin.html sets cookie admin_token; also accept ?token= query
    const m = document.cookie.match(/admin_token=([^;]+)/);
    if (m) return m[1];
    const u = new URLSearchParams(window.location.search);
    return u.get('token') || '';
  }

  function tokenQS() {
    const t = adminToken();
    return t ? `?token=${encodeURIComponent(t)}` : '';
  }

  async function apiGet(path) {
    const sep = path.includes('?') ? '&' : '?';
    const t = adminToken();
    const url = t ? `${path}${sep}token=${encodeURIComponent(t)}` : path;
    const resp = await fetch(url, { credentials: 'include' });
    if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
    return resp.json();
  }

  async function apiPost(path, body = {}) {
    const sep = path.includes('?') ? '&' : '?';
    const t = adminToken();
    const url = t ? `${path}${sep}token=${encodeURIComponent(t)}` : path;
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      credentials: 'include',
    });
    let data = null;
    try { data = await resp.json(); } catch (_) {}
    if (!resp.ok) throw new Error(data?.error || `${path} → ${resp.status}`);
    return data;
  }

  async function apiDelete(path) {
    const sep = path.includes('?') ? '&' : '?';
    const t = adminToken();
    const url = t ? `${path}${sep}token=${encodeURIComponent(t)}` : path;
    const resp = await fetch(url, { method: 'DELETE', credentials: 'include' });
    let data = null;
    try { data = await resp.json(); } catch (_) {}
    if (!resp.ok) throw new Error(data?.error || `${path} → ${resp.status}`);
    return data;
  }

  function shortId(id, n = 12) {
    if (!id) return '';
    return id.length > n ? id.slice(0, n) + '…' : id;
  }

  function fmtDate(s) {
    if (!s) return '—';
    try { return new Date(s).toLocaleString('fr-FR'); } catch (_) { return s; }
  }

  function trustBadge(level) {
    const colors = { 0: '#777', 1: '#f0b429', 2: '#28a745', 3: '#8b5cf6' };
    const labels = { 0: 'unknown', 1: 'known', 2: 'trusted', 3: 'bonded' };
    const col = colors[level] || '#777';
    return h('span', {
      class: 'fed-badge',
      style: `background:${col};color:#fff;padding:2px 8px;border-radius:10px;font-size:0.75rem;font-weight:600;`,
    }, `${level} ${labels[level] || '?'}`);
  }

  function copyBtn(text) {
    return h('button', {
      class: 'fed-copy',
      title: 'copier',
      style: 'background:transparent;border:1px solid var(--border);color:var(--text2);padding:1px 6px;border-radius:4px;cursor:pointer;font-size:0.7rem;margin-left:4px;',
      onclick: () => navigator.clipboard.writeText(text).then(() => {}),
    }, '⧉');
  }

  // ---- Sections ----

  function renderIdentityCard(self) {
    if (!self || self.mode === 'off') {
      return h('div', { class: 'fed-card', style: 'padding:1rem;background:var(--bg2);border:1px solid var(--border);border-radius:8px;margin-bottom:1rem;' },
        h('h3', { style: 'margin:0 0 0.5rem 0;' }, 'Identité'),
        h('div', { style: 'color:var(--text2);' }, self?.mode === 'off' ? 'Federation OFF (env IAMINE_FED)' : '—')
      );
    }
    const caps = Array.isArray(self.capabilities) ? self.capabilities : [];
    return h('div', {
      class: 'fed-card',
      style: 'padding:1rem;background:var(--bg2);border:1px solid var(--border);border-radius:8px;margin-bottom:1rem;',
    },
      h('h3', { style: 'margin:0 0 0.75rem 0;display:flex;align-items:center;gap:0.5rem;' },
        'Identité',
        h('span', {
          style: `background:${self.mode === 'active' ? '#28a745' : '#f0b429'};color:#fff;padding:2px 10px;border-radius:12px;font-size:0.75rem;`,
        }, self.mode.toUpperCase())
      ),
      h('div', { style: 'display:grid;grid-template-columns:120px 1fr;gap:0.5rem 1rem;font-size:0.85rem;' },
        h('div', { style: 'color:var(--text2);' }, 'atom_id'),
        h('div', { style: 'font-family:monospace;' }, shortId(self.atom_id, 24), copyBtn(self.atom_id)),

        h('div', { style: 'color:var(--text2);' }, 'name'),
        h('div', {}, self.name || '—'),

        h('div', { style: 'color:var(--text2);' }, 'url'),
        h('div', { style: 'font-family:monospace;' }, self.url || '—'),

        h('div', { style: 'color:var(--text2);' }, 'molecule'),
        h('div', {}, self.molecule_id || 'standalone'),

        h('div', { style: 'color:var(--text2);' }, 'pubkey'),
        h('div', { style: 'font-family:monospace;font-size:0.75rem;' }, shortId(self.pubkey_hex, 32), copyBtn(self.pubkey_hex || '')),

        h('div', { style: 'color:var(--text2);' }, 'hop_max'),
        h('div', {}, String(self.hop_max ?? '—')),

        h('div', { style: 'color:var(--text2);' }, 'capabilities'),
        h('div', {}, caps.length ? caps.map(c => `${c.kind || '?'}:${c.model || c.backend || '?'}`).join(', ') : '(none)')
      )
    );
  }

  function peerRow(peer) {
    return h('tr', {},
      h('td', {}, peer.name || '—'),
      h('td', {}, trustBadge(peer.trust_level || 0)),
      h('td', { style: 'font-family:monospace;font-size:0.75rem;' },
        shortId(peer.atom_id, 16),
        copyBtn(peer.atom_id),
      ),
      h('td', { style: 'font-family:monospace;font-size:0.75rem;' }, peer.url || '—'),
      h('td', { style: 'font-size:0.75rem;' }, peer.last_seen ? fmtDate(peer.last_seen) : '—'),
      h('td', { style: 'font-size:0.75rem;' }, Array.isArray(peer.capabilities) ? `${peer.capabilities.length} caps` : '—'),
      h('td', {},
        h('button', {
          class: 'fed-action',
          style: 'background:#28a745;color:#fff;border:none;padding:2px 8px;margin-right:4px;border-radius:4px;cursor:pointer;font-size:0.75rem;',
          title: 'promote',
          onclick: () => fedPromote(peer.atom_id),
        }, '↑'),
        h('button', {
          class: 'fed-action',
          style: 'background:#f0b429;color:#fff;border:none;padding:2px 8px;margin-right:4px;border-radius:4px;cursor:pointer;font-size:0.75rem;',
          title: 'demote',
          onclick: () => fedDemote(peer.atom_id),
        }, '↓'),
        h('button', {
          class: 'fed-action',
          style: 'background:#dc3545;color:#fff;border:none;padding:2px 8px;margin-right:4px;border-radius:4px;cursor:pointer;font-size:0.75rem;',
          title: 'revoke (soft)',
          onclick: () => fedRevoke(peer.atom_id),
        }, '✕'),
        h('button', {
          class: 'fed-action',
          style: 'background:#4a0e0e;color:#fff;border:1px solid #dc3545;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.75rem;',
          title: 'hard delete (refused if peer has settlements)',
          onclick: () => fedDelete(peer.atom_id, peer.name),
        }, '🗑')
      )
    );
  }

  function renderPeersTable(peers) {
    const wrap = h('div', {
      class: 'fed-card',
      style: 'padding:1rem;background:var(--bg2);border:1px solid var(--border);border-radius:8px;margin-bottom:1rem;',
    });
    const headerRow = h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem;' },
      h('h3', { style: 'margin:0;' }, `Peers (${peers.length})`),
      h('button', {
        style: 'background:transparent;color:var(--text2);border:1px solid var(--border);padding:3px 10px;border-radius:4px;cursor:pointer;font-size:0.75rem;',
        title: 'Hard-delete tous les peers revoked',
        onclick: () => fedPurgeRevoked(),
      }, 'Purge revoked')
    );
    wrap.appendChild(headerRow);
    if (!peers.length) {
      wrap.appendChild(h('div', { style: 'color:var(--text2);font-size:0.85rem;' }, '(aucun peer connu)'));
      return wrap;
    }
    const table = h('table', { style: 'width:100%;font-size:0.85rem;' },
      h('thead', {},
        h('tr', { style: 'text-align:left;border-bottom:1px solid var(--border);' },
          h('th', { style: 'padding:0.4rem 0.5rem;' }, 'Name'),
          h('th', { style: 'padding:0.4rem 0.5rem;' }, 'Trust'),
          h('th', { style: 'padding:0.4rem 0.5rem;' }, 'Atom ID'),
          h('th', { style: 'padding:0.4rem 0.5rem;' }, 'URL'),
          h('th', { style: 'padding:0.4rem 0.5rem;' }, 'Last seen'),
          h('th', { style: 'padding:0.4rem 0.5rem;' }, 'Caps'),
          h('th', { style: 'padding:0.4rem 0.5rem;' }, 'Actions')
        )
      ),
      h('tbody', {}, peers.map(peerRow))
    );
    wrap.appendChild(table);
    return wrap;
  }

  function renderAddPeerForm() {
    const urlInput = h('input', {
      type: 'text',
      id: 'fed-add-url',
      placeholder: 'https://peer.example.org',
      style: 'flex:1;padding:0.4rem;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;font-family:monospace;',
    });
    const nameInput = h('input', {
      type: 'text',
      id: 'fed-add-name',
      placeholder: 'name (optional)',
      style: 'width:160px;padding:0.4rem;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;',
    });
    const recipCheck = h('input', { type: 'checkbox', id: 'fed-add-reciprocate' });
    const label = h('label', { style: 'display:flex;align-items:center;gap:0.3rem;font-size:0.85rem;color:var(--text2);' }, recipCheck, 'reciprocate');
    const btn = h('button', {
      style: 'background:var(--accent);color:#fff;border:none;padding:0.5rem 1.2rem;border-radius:4px;cursor:pointer;font-weight:600;',
      onclick: async () => {
        const url = urlInput.value.trim();
        if (!url) return alert('URL requise');
        try {
          btn.disabled = true;
          btn.textContent = '…';
          const res = await apiPost('/v1/federation/admin/register', {
            url, reciprocate: recipCheck.checked, name: nameInput.value.trim() || undefined,
          });
          alert(`OK: ${res.target_name} (${shortId(res.target_atom_id, 16)})\ntrust=${res.target_trust_level_on_us} sig_verified=${res.signature_verified_by_target}`);
          urlInput.value = ''; nameInput.value = ''; recipCheck.checked = false;
          await loadFederation();
        } catch (e) {
          alert('Erreur: ' + e.message);
        } finally {
          btn.disabled = false;
          btn.textContent = 'Register';
        }
      },
    }, 'Register');

    return h('div', {
      class: 'fed-card',
      style: 'padding:1rem;background:var(--bg2);border:1px solid var(--border);border-radius:8px;margin-bottom:1rem;',
    },
      h('h3', { style: 'margin:0 0 0.75rem 0;' }, 'Ajouter un peer'),
      h('div', { style: 'display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;' },
        urlInput, nameInput, label, btn
      )
    );
  }

  function renderHeartbeat(hb) {
    const missed = hb?.missed_beats || {};
    const lastSuccess = hb?.last_success_at || {};
    const peersInfo = Object.keys({ ...missed, ...lastSuccess });

    const body = peersInfo.length
      ? h('table', { style: 'width:100%;font-size:0.8rem;' },
          h('thead', {},
            h('tr', { style: 'text-align:left;border-bottom:1px solid var(--border);' },
              h('th', { style: 'padding:0.3rem 0.5rem;' }, 'Peer atom_id'),
              h('th', { style: 'padding:0.3rem 0.5rem;' }, 'Last success'),
              h('th', { style: 'padding:0.3rem 0.5rem;' }, 'Missed beats')
            )
          ),
          h('tbody', {},
            peersInfo.map(aid => h('tr', {},
              h('td', { style: 'font-family:monospace;padding:0.3rem 0.5rem;' }, shortId(aid, 16)),
              h('td', { style: 'padding:0.3rem 0.5rem;' }, lastSuccess[aid] ? fmtDate(lastSuccess[aid]) : '—'),
              h('td', { style: `padding:0.3rem 0.5rem;color:${(missed[aid] || 0) > 0 ? '#dc3545' : '#28a745'};` }, String(missed[aid] || 0))
            ))
          )
        )
      : h('div', { style: 'color:var(--text2);font-size:0.85rem;' }, '(pas de peers bonded pinging)');

    return h('div', {
      class: 'fed-card',
      style: 'padding:1rem;background:var(--bg2);border:1px solid var(--border);border-radius:8px;margin-bottom:1rem;',
    },
      h('h3', { style: 'margin:0 0 0.75rem 0;' }, `Heartbeat (interval ${hb?.interval_sec || '?'}s, unreachable ${hb?.unreachable_after_sec || '?'}s)`),
      body
    );
  }

  function renderLedger(rows) {
    const pendingCount = rows.filter(r => r.worker_sig_null).length;
    const wrap = h('div', {
      class: 'fed-card',
      style: 'padding:1rem;background:var(--bg2);border:1px solid var(--border);border-radius:8px;margin-bottom:1rem;',
    });
    wrap.appendChild(h('h3', { style: 'margin:0 0 0.4rem 0;' }, `Revenue ledger (${rows.length} rows)`));
    if (pendingCount > 0) {
      wrap.appendChild(h('div', {
        style: 'color:#f0b429;font-size:0.8rem;margin-bottom:0.5rem;',
      }, `⚠ ${pendingCount} rows avec worker_sig=NULL (pending M7-worker backfill — settlement M10 les rejettera)`));
    }
    if (!rows.length) {
      wrap.appendChild(h('div', { style: 'color:var(--text2);font-size:0.85rem;' }, '(aucune entrée)'));
      return wrap;
    }
    const table = h('table', { style: 'width:100%;font-size:0.75rem;' },
      h('thead', {},
        h('tr', { style: 'text-align:left;border-bottom:1px solid var(--border);' },
          h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Job'),
          h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Origin→Exec'),
          h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Worker'),
          h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Model'),
          h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Tokens'),
          h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Credits (W/E/O/T)'),
          h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Sig'),
          h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Created')
        )
      ),
      h('tbody', {},
        rows.map(r => h('tr', { style: 'border-bottom:1px solid var(--border);' },
          h('td', { style: 'font-family:monospace;padding:0.3rem 0.4rem;' }, shortId(r.job_id, 20)),
          h('td', { style: 'font-family:monospace;padding:0.3rem 0.4rem;font-size:0.7rem;' },
            `${shortId(r.origin_pool_id, 8)}→${shortId(r.exec_pool_id, 8)}`),
          h('td', { style: 'padding:0.3rem 0.4rem;' }, r.worker_id || '—'),
          h('td', { style: 'padding:0.3rem 0.4rem;' }, r.model || '—'),
          h('td', { style: 'padding:0.3rem 0.4rem;' }, `${r.tokens_in}/${r.tokens_out}`),
          h('td', { style: 'padding:0.3rem 0.4rem;' },
            `${r.credits_worker}/${r.credits_exec}/${r.credits_origin}/${r.credits_treasury}`),
          h('td', { style: `padding:0.3rem 0.4rem;color:${r.worker_sig_null ? '#f0b429' : '#28a745'};font-weight:600;` },
            r.worker_sig_null ? '∅ PENDING' : '✓'),
          h('td', { style: 'padding:0.3rem 0.4rem;font-size:0.7rem;' }, fmtDate(r.created_at))
        ))
      )
    );
    wrap.appendChild(table);
    return wrap;
  }

  // ---- Settlement panel (M10-scaffold) ----

  function renderSettlement(state) {
    const wrap = h('div', {
      class: 'fed-card',
      style: 'padding:1rem;background:var(--bg2);border:1px solid rgba(139,92,246,0.4);border-radius:8px;margin-bottom:1rem;',
    });

    wrap.appendChild(h('h3', { style: 'margin:0 0 0.5rem 0;display:flex;align-items:center;gap:0.5rem;' },
      'Settlement',
      h('span', {
        style: `background:${state?.enabled ? '#8b5cf6' : '#777'};color:#fff;padding:2px 10px;border-radius:12px;font-size:0.7rem;`,
      }, state?.enabled ? String(state?.mode || 'dry_run').toUpperCase() : 'DISABLED'),
      h('span', {
        style: 'background:#f0b429;color:#000;padding:2px 10px;border-radius:12px;font-size:0.65rem;font-weight:700;',
        title: 'Scaffold: non-authoritative, M10-active pending',
      }, 'SCAFFOLD')
    ));

    wrap.appendChild(h('div', { style: 'font-size:0.75rem;color:var(--text2);margin-bottom:0.5rem;' },
      'authoritative: ',
      h('strong', { style: `color:${state?.authoritative ? '#28a745' : '#f0b429'};` }, String(state?.authoritative)),
      ` | period: ${state?.period_sec || '?'}s | kill_switch: `,
      h('strong', { style: `color:${state?.kill_switch ? '#dc3545' : '#28a745'};` }, String(state?.kill_switch || false))
    ));

    const rows = state?.rows || [];
    if (!rows.length) {
      wrap.appendChild(h('div', { style: 'color:var(--text2);font-size:0.85rem;padding:0.5rem 0;' },
        state?.enabled
          ? '(loop actif, aucune proposition encore)'
          : '(settlement disabled — SETTLEMENT_ENABLED=false)'));
    } else {
      const table = h('table', { style: 'width:100%;font-size:0.75rem;' },
        h('thead', {},
          h('tr', { style: 'text-align:left;border-bottom:1px solid var(--border);' },
            h('th', { style: 'padding:0.3rem 0.4rem;' }, '#'),
            h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Peer'),
            h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Period'),
            h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Net'),
            h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Status'),
            h('th', { style: 'padding:0.3rem 0.4rem;' }, 'Proposed')
          )
        ),
        h('tbody', {},
          rows.map(r => h('tr', { style: 'border-bottom:1px solid var(--border);' },
            h('td', { style: 'padding:0.3rem 0.4rem;' }, String(r.id)),
            h('td', { style: 'font-family:monospace;padding:0.3rem 0.4rem;font-size:0.7rem;' }, shortId(r.peer_id, 12)),
            h('td', { style: 'padding:0.3rem 0.4rem;font-size:0.7rem;' },
              (r.period_start || '').slice(0, 10), ' → ', (r.period_end || '').slice(0, 10)),
            h('td', { style: `padding:0.3rem 0.4rem;color:${r.net_credits >= 0 ? '#28a745' : '#dc3545'};font-weight:600;` },
              (r.net_credits >= 0 ? '+' : '') + String(r.net_credits)),
            h('td', { style: 'padding:0.3rem 0.4rem;' },
              h('span', {
                style: `background:${r.status === 'proposed' ? '#f0b429' : '#777'};color:#000;padding:1px 6px;border-radius:8px;font-size:0.65rem;`,
              }, r.status)),
            h('td', { style: 'padding:0.3rem 0.4rem;font-size:0.7rem;' }, fmtDate(r.proposed_at))
          ))
        )
      );
      wrap.appendChild(table);
    }

    wrap.appendChild(h('div', { style: 'margin-top:0.6rem;font-size:0.7rem;color:var(--text2);font-style:italic;' },
      '⚠ Scaffold only — treasury (10%) EXCLUDED from aggregation. No real credit transfer. ',
      'Rows with worker_sig=NULL filtered out (pending M7-worker).'));

    return wrap;
  }

  // ---- Actions ----

  async function fedPromote(atom_id) {
    try {
      const res = await apiPost(`/v1/federation/peers/${atom_id}/promote`, { target_level: 2 });
      if (res.ok) { await loadFederation(); }
      else { alert(`promote failed: ${res.message}`); }
    } catch (e) { alert('promote error: ' + e.message); }
  }

  async function fedDemote(atom_id) {
    try {
      const res = await apiPost(`/v1/federation/peers/${atom_id}/demote`, { target_level: 1 });
      if (res.ok) { await loadFederation(); }
      else { alert(`demote failed: ${res.message}`); }
    } catch (e) { alert('demote error: ' + e.message); }
  }

  async function fedRevoke(atom_id) {
    if (!confirm('Révoquer ce peer (soft, réversible en promote) ?')) return;
    try {
      const res = await apiPost(`/v1/federation/peers/${atom_id}/revoke`);
      if (res.ok) { await loadFederation(); }
      else { alert(`revoke failed: ${res.message}`); }
    } catch (e) { alert('revoke error: ' + e.message); }
  }

  async function fedDelete(atom_id, name) {
    const msg = `Hard-DELETE le peer "${name || atom_id.slice(0,16)}" ?\n\nIrreversible. Refuse si le peer a des settlements enregistres.`;
    if (!confirm(msg)) return;
    try {
      const res = await apiDelete(`/v1/federation/peers/${atom_id}`);
      if (res.ok) { await loadFederation(); }
      else { alert(`delete failed: ${res.error || 'unknown'}`); }
    } catch (e) { alert('delete error: ' + e.message); }
  }

  async function fedPurgeRevoked() {
    if (!confirm('Hard-DELETE tous les peers marques revoked ?\n\nIrreversible. Refuse pour ceux avec settlements.')) return;
    try {
      const resp = await apiGet('/v1/federation/peers?include_revoked=1');
      const peers = resp.peers || [];
      const revoked = peers.filter(p => p.revoked_at);
      if (!revoked.length) { alert('Aucun peer revoked.'); return; }
      let ok = 0, fail = 0;
      for (const p of revoked) {
        try {
          await apiDelete(`/v1/federation/peers/${p.atom_id}`);
          ok++;
        } catch (_) { fail++; }
      }
      alert(`Purge: ${ok} deleted` + (fail ? `, ${fail} failed (settlements?)` : ''));
      await loadFederation();
    } catch (e) { alert('purge error: ' + e.message); }
  }

  // ---- Main loader ----


  // --- M11.1 replication state + account replication log ----

  function renderReplicationState(rstate) {
    if (!rstate) return h('div', { class: 'fed-section' });
    var wrap = h('div', { class: 'fed-section', style: 'margin-top:1.5rem;padding:1rem;background:var(--bg2,#1a1a1f);border:1px solid var(--border,#333);border-radius:8px;' });
    wrap.appendChild(h('h3', { style: 'margin:0 0 0.75rem;font-size:1rem;color:#8b5cf6;' }, 'M11 Replication state'));

    var flags = (rstate.flags) || {};
    var quorum = (rstate.account_creation_quorum) || {};
    var state = (rstate.state) || {};
    var queue = (rstate.queue) || {};

    function kv(k, v, color) {
      return h('div', { style: 'display:flex;justify-content:space-between;padding:0.2rem 0;border-bottom:1px solid rgba(255,255,255,0.05);' },
        h('span', { style: 'opacity:0.6;font-size:0.85rem;' }, k),
        h('span', { style: 'font-family:ui-monospace,monospace;font-size:0.85rem;color:' + (color || 'var(--text,#e0e0e0)') }, String(v))
      );
    }

    function boolBadge(b) {
      return b ? '✓ on' : '○ off';
    }

    var col1 = h('div', { style: 'display:flex;flex-direction:column;gap:0;flex:1;min-width:260px;' },
      h('div', { style: 'font-size:0.8rem;text-transform:uppercase;letter-spacing:0.05em;opacity:0.55;margin-bottom:0.4rem;' }, 'Flags'),
      kv('REPLICATION_ENABLED', boolBadge(flags.REPLICATION_ENABLED), flags.REPLICATION_ENABLED ? '#28a745' : '#777'),
      kv('ACCOUNT_CREATION_QUORUM_ENABLED', boolBadge(flags.ACCOUNT_CREATION_QUORUM_ENABLED), flags.ACCOUNT_CREATION_QUORUM_ENABLED ? '#28a745' : '#777'),
      kv('FS_KILL_SWITCH', boolBadge(flags.FS_KILL_SWITCH), flags.FS_KILL_SWITCH ? '#dc3545' : '#777')
    );

    var col2 = h('div', { style: 'display:flex;flex-direction:column;gap:0;flex:1;min-width:260px;' },
      h('div', { style: 'font-size:0.8rem;text-transform:uppercase;letter-spacing:0.05em;opacity:0.55;margin-bottom:0.4rem;' }, 'Account creation quorum'),
      kv('active', boolBadge(quorum.active), quorum.active ? '#28a745' : '#f0b429'),
      kv('reachable_count', quorum.reachable_count || 0),
      kv('total_molecule_size', quorum.total_molecule_size || 0),
      kv('required', quorum.required || 0),
      kv('would_block_in_phase_2', boolBadge(quorum.would_block_in_phase_2), quorum.would_block_in_phase_2 ? '#dc3545' : '#28a745'),
      kv('phase', quorum.phase || 1)
    );

    var col3 = h('div', { style: 'display:flex;flex-direction:column;gap:0;flex:1;min-width:260px;' },
      h('div', { style: 'font-size:0.8rem;text-transform:uppercase;letter-spacing:0.05em;opacity:0.55;margin-bottom:0.4rem;' }, 'Replication queue'),
      kv('pending', queue.pending || 0),
      kv('in_progress', queue.in_progress || 0),
      kv('done', queue.done || 0),
      kv('failed', queue.failed || 0, (queue.failed || 0) > 0 ? '#dc3545' : 'var(--text,#e0e0e0)'),
      kv('rebuild_status', state.rebuild_status || '—')
    );

    var grid = h('div', { style: 'display:flex;gap:1.5rem;flex-wrap:wrap;' }, col1, col2, col3);
    wrap.appendChild(grid);
    return wrap;
  }

  function renderAccountReplicationLog(rows) {
    var wrap = h('div', { class: 'fed-section', style: 'margin-top:1.5rem;padding:1rem;background:var(--bg2,#1a1a1f);border:1px solid var(--border,#333);border-radius:8px;' });
    wrap.appendChild(h('h3', { style: 'margin:0 0 0.75rem;font-size:1rem;color:#8b5cf6;' }, 'M11.1 Account replication log (last ' + (rows.length || 0) + ')'));

    if (!rows || rows.length === 0) {
      wrap.appendChild(h('div', { style: 'padding:1rem;opacity:0.5;text-align:center;' }, 'No replication events yet'));
      return wrap;
    }

    var table = h('table', { style: 'width:100%;font-size:0.82rem;border-collapse:collapse;' });
    var thead = h('thead', {},
      h('tr', { style: 'border-bottom:1px solid rgba(255,255,255,0.15);' },
        h('th', { style: 'text-align:left;padding:0.4rem 0.5rem;opacity:0.6;font-weight:500;' }, 'Time'),
        h('th', { style: 'text-align:left;padding:0.4rem 0.5rem;opacity:0.6;font-weight:500;' }, 'Dir'),
        h('th', { style: 'text-align:left;padding:0.4rem 0.5rem;opacity:0.6;font-weight:500;' }, 'Account'),
        h('th', { style: 'text-align:left;padding:0.4rem 0.5rem;opacity:0.6;font-weight:500;' }, 'Peer'),
        h('th', { style: 'text-align:left;padding:0.4rem 0.5rem;opacity:0.6;font-weight:500;' }, 'Status')
      )
    );
    table.appendChild(thead);
    var tbody = h('tbody', {});
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var dirColor = r.direction === 'push' ? '#00d4ff' : '#28a745';
      var statusColor = r.status === 'ack' ? '#28a745' : '#dc3545';
      tbody.appendChild(
        h('tr', { style: 'border-bottom:1px solid rgba(255,255,255,0.04);' },
          h('td', { style: 'padding:0.35rem 0.5rem;font-family:ui-monospace,monospace;font-size:0.75rem;opacity:0.7;' }, fmtDate(r.created_at)),
          h('td', { style: 'padding:0.35rem 0.5rem;color:' + dirColor + ';font-weight:600;' }, r.direction),
          h('td', { style: 'padding:0.35rem 0.5rem;font-family:ui-monospace,monospace;font-size:0.78rem;' }, shortId(r.account_id, 16)),
          h('td', { style: 'padding:0.35rem 0.5rem;font-family:ui-monospace,monospace;font-size:0.78rem;' }, shortId(r.peer_atom_id, 16)),
          h('td', { style: 'padding:0.35rem 0.5rem;color:' + statusColor + ';' }, r.status)
        )
      );
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  async function loadFederation() {
    const container = $('#fed-root');
    if (!container) return;

    container.innerHTML = '<div style="color:var(--text2);padding:1rem;">chargement…</div>';

    try {
      const [info, peersResp, hb, ledger, settlement, replState, acctLog] = await Promise.all([
        apiGet('/v1/federation/info').catch(() => ({ mode: 'off' })),
        apiGet('/v1/federation/peers').catch(() => ({ peers: [] })),
        apiGet('/v1/federation/heartbeat').catch(() => null),
        apiGet('/v1/federation/ledger?limit=30').catch(() => ({ rows: [] })),
        apiGet('/v1/federation/settlement/state?limit=20').catch(() => null),
        apiGet('/v1/federation/replication/state').catch(() => null),
        apiGet('/v1/federation/accounts/replication-log?limit=25').catch(() => ({ rows: [] })),
      ]);

      FED.state.self = info;
      FED.state.peers = peersResp.peers || [];
      FED.state.heartbeat = hb;
      FED.state.ledger = ledger.rows || [];
      FED.state.settlement = settlement;
      FED.state.replication = replState;
      FED.state.acctLog = acctLog.rows || [];

      container.innerHTML = '';
      container.appendChild(renderIdentityCard(info));
      if (info.mode !== 'off') {
        container.appendChild(renderAddPeerForm());
        container.appendChild(renderPeersTable(FED.state.peers));
        container.appendChild(renderReplicationState(FED.state.replication));
        container.appendChild(renderAccountReplicationLog(FED.state.acctLog));
        container.appendChild(renderHeartbeat(FED.state.heartbeat));
        container.appendChild(renderLedger(FED.state.ledger));
        container.appendChild(renderSettlement(FED.state.settlement));
      }
    } catch (e) {
      container.innerHTML = `<div style="color:#dc3545;padding:1rem;">Erreur: ${e.message}</div>`;
    }
  }

  // Expose for admin.html showTab() integration
  window.loadFederation = loadFederation;
  window.fedPromote = fedPromote;
  window.fedDemote = fedDemote;
  window.fedRevoke = fedRevoke;
  window.fedDelete = fedDelete;
  window.fedPurgeRevoked = fedPurgeRevoked;
})();
