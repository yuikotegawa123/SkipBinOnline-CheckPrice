// ==UserScript==
// @name         BookABin Rate Updater
// @namespace    bookabin-rate-updater
// @version      6.3
// @description  Auto-navigates per waste type using pasted Update Price table
// @match        *://*.bookabin.com.au/*
// @run-at       document-end
// @grant        none
// ==/UserScript==

(function () {

    var WASTE_URLS = {
        'General Waste':      'https://www.bookabin.com.au/supplier/rates_manage.aspx',
        'Mixed Heavy Waste':  'https://www.bookabin.com.au/supplier/rates_manage_mixedheavy.aspx',
        'Cleanfill/Hardfill':'https://www.bookabin.com.au/supplier/rates_manage_clean.aspx',
        'Green Garden Waste': 'https://www.bookabin.com.au/supplier/rates_manage_green.aspx',
        'Soil / Dirt':        'https://www.bookabin.com.au/supplier/rates_manage_dirt.aspx',
    };
    var STATE_KEY = 'bb_updater_state';
    var stopFlag  = false;

    // ── Helpers ──────────────────────────────────────────────────────────────

    function isRatesPage() {
        return /rates_manage/i.test(decodeURIComponent(window.location.href));
    }

    function currentPageMatchesGroup(group) {
        var url = group.url || (WASTE_URLS[group.wasteType] ? WASTE_URLS[group.wasteType] : null);
        if (!url) return true; // no URL to check — assume correct
        // Compare path only (ignore query string)
        var currentPath = window.location.pathname.toLowerCase();
        var targetPath  = url.replace(/^https?:\/\/[^/]+/i, '').replace(/\?.*$/, '').toLowerCase();
        return currentPath === targetPath;
    }

    function wait(ms) { return new Promise(function(r){ setTimeout(r, ms); }); }

    function waitFor(fn, ms) {
        return new Promise(function(res, rej) {
            var end = Date.now() + (ms || 10000);
            (function check() {
                var r = fn(); if (r) return res(r);
                if (Date.now() > end) return rej(new Error('timeout'));
                setTimeout(check, 300);
            })();
        });
    }

    function getAllTableRows() {
        // Try exact ID first, then case-insensitive search, then all table rows on page
        var tbody = document.querySelector('table#dltRates tbody');
        if (!tbody) {
            var tables = document.querySelectorAll('table');
            for (var t = 0; t < tables.length; t++) {
                var id = (tables[t].id || '').toLowerCase();
                if (id.indexOf('rate') !== -1) { tbody = tables[t].querySelector('tbody') || tables[t]; break; }
            }
        }
        if (!tbody) {
            // Last resort: all tr elements on the page
            return Array.prototype.slice.call(document.querySelectorAll('tr'));
        }
        return Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    }

    function waitForRows() {
        return waitFor(function() {
            return getAllTableRows().length > 0 ? true : null;
        }, 15000);
    }

    // Selector for any edit-like button/link
    function findEditBtn(row) {
        return row.querySelector(
            'input[type="image"][alt="Edit Row"], input[type="image"][id*="Linkbutton1"], ' +
            'input[type="image"][src*="edit" i], input[type="image"][alt*="edit" i], input[type="image"][title*="edit" i], ' +
            'a[title*="edit" i], button[title*="edit" i], img[src*="edit" i][onclick], input[type="image"]'
        );
    }

    // Find the row that contains the Edit button for a given size.
    function findRowForSize(sz) {
        // Normalise: strip trailing "m3", "m³", "cubic metres" etc. → bare number (e.g. "2m3" → "2", "7.5" → "7.5")
        var numStr = sz.toString().replace(/\s*m(?:3|³|etres?|cubic\s*metres?)?.*$/i, '').trim();
        var szPat = new RegExp('(?<![\\d.])' + numStr.replace('.', '\\.') + '(?![\\d.])', 'i');
        var rows = getAllTableRows();

        if (rows.length === 0) {
            log('  [debug] No table rows found on page!', '#fab387');
            return null;
        }

        for (var i = 0; i < rows.length; i++) {
            var txt = rows[i].innerText.replace(/\s+/g, ' ').trim();
            if (!szPat.test(txt)) continue;
            // Must contain "cubic" to avoid matching date-header rows (e.g. "Sat 11 Apr")
            if (!/cubic/i.test(txt)) continue;
            // Check this row and up to 4 following rows (Price row + Stock row structure)
            var limit = Math.min(rows.length - 1, i + 4);
            for (var j = i; j <= limit; j++) {
                var btn = findEditBtn(rows[j]);
                if (btn) return rows[j];
            }
            // Nothing found nearby — log and return size row for caller to diagnose
            log('  [debug] sz=' + sz + ' (num=' + numStr + ') row found but no edit btn in rows ' + i + '-' + limit + '. Row HTML: ' + rows[i].innerHTML.substring(0, 300), '#fab387');
            return rows[i];
        }

        // Log sample rows to help diagnose
        var sample = rows.slice(0, Math.min(5, rows.length)).map(function(r) { return '"' + r.innerText.replace(/\s+/g, ' ').trim().substring(0, 80) + '"'; }).join(' | ');
        log('  [debug] sz=' + sz + ' (num=' + numStr + ') not found. Sample rows: ' + sample, '#fab387');
        return null;
    }

    // ── Paste Parser ─────────────────────────────────────────────────────────
    // Accepts JSON copied from Streamlit "Copy as JSON":
    //   { "General Waste": { "2": 208, "3": 298, ... }, ... }

    function parsePaste(text) {
        text = text.trim();
        if (!text) return null;
        try {
            var data = JSON.parse(text);
            var order = Object.keys(data);
            if (!order.length) return null;
            return order.map(function(wt) {
                var sizes = data[wt];
                var url   = sizes['_url'] || null;
                var items = Object.keys(sizes).filter(function(k) { return k !== '_url'; }).map(function(sz) {
                    return { size: sz, price: parseInt(sizes[sz], 10) };
                }).filter(function(it) { return !isNaN(it.price); });
                return { wasteType: wt, url: url, items: items };
            });
        } catch(e) {
            return null;
        }
    }

    // ── SessionStorage state ──────────────────────────────────────────────────

    function saveState(st) {
        try { sessionStorage.setItem(STATE_KEY, JSON.stringify(st)); } catch(e) {}
    }
    function loadState() {
        try { var s = sessionStorage.getItem(STATE_KEY); return s ? JSON.parse(s) : null; } catch(e) { return null; }
    }
    function clearState() {
        try { sessionStorage.removeItem(STATE_KEY); } catch(e) {}
    }

    // ── Logging ───────────────────────────────────────────────────────────────

    function log(msg, color) {
        var el = document.getElementById('bb-log');
        if (el) {
            el.innerHTML += '<div style="color:' + (color || '#f9e2af') + '">' + msg + '</div>';
            el.scrollTop = el.scrollHeight;
        }
        console.log('[RateUpdater] ' + msg);
        try {
            var st = loadState();
            if (st) { st.logs = st.logs || []; st.logs.push({ msg: msg, color: color || '#f9e2af' }); saveState(st); }
        } catch(e) {}
    }

    // ── Update one group's items (current page, size-only matching) ───────────

    async function updateGroupItems(items, startIdx) {
        var done = 0;
        for (var i = (startIdx || 0); i < items.length; i++) {
            if (stopFlag) return done;
            var sz = items[i].size, price = items[i].price;

            var row = findRowForSize(sz);
            if (!row) { log('  ' + sz + 'm3: row not found - skipping.', '#f38ba8'); continue; }

            var editBtn = findEditBtn(row);
            if (!editBtn) {
                log('  ' + sz + 'm3: Edit button not found. Row HTML: ' + row.innerHTML.substring(0, 200), '#f38ba8');
                continue;
            }

            // Derive ctl prefix from edit button id: "dltRates_ctl01_Linkbutton1" -> "dltRates_ctl01"
            var ctlPrefix = null;
            if (editBtn.id) {
                var m = editBtn.id.match(/^(dltRates_ctl\d+)_/i);
                if (m) ctlPrefix = m[1];
            }

            log('  ' + sz + 'm3 -> $' + price);
            editBtn.scrollIntoView({ block: 'center', behavior: 'smooth' });
            await wait(400);
            editBtn.click();

            // Wait for the save button to appear (indicates row is now in edit mode)
            var saveBtn;
            try {
                saveBtn = await waitFor(function() {
                    if (ctlPrefix) {
                        var b = document.getElementById(ctlPrefix + '_lbtratesave');
                        if (b) return b;
                    }
                    return document.querySelector('input[id*="lbtratesave"], input[alt="Update Row"]') || null;
                }, 8000);
            } catch(e) {
                log('  ' + sz + 'm3: timed out waiting for edit mode - skipping.', '#f38ba8');
                var cc = document.querySelector('input[alt="Cancel"], input[title="Cancel"]');
                if (cc) cc.click();
                continue;
            }

            // Fill only the FIRST date-column input (next to Base, tabindex="3") in the edit row
            var priceInput = null;
            var editTr = saveBtn.closest('tr');
            if (editTr) {
                priceInput = editTr.querySelector('input.rateinput[tabindex="3"]') ||
                    editTr.querySelector('input.rateinput');
            }
            if (!priceInput) priceInput = document.querySelector('input.rateinput[tabindex="3"]') ||
                document.querySelector('input.rateinput');

            if (!priceInput) {
                log('  ' + sz + ': price input not found - cancelling.', '#f38ba8');
                var cn = document.querySelector('input[alt="Cancel"], input[title="Cancel"]');
                if (cn) cn.click();
                continue;
            }

            var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            priceInput.focus();
            priceInput.select();
            setter.call(priceInput, String(price));
            priceInput.dispatchEvent(new Event('input',  { bubbles: true }));
            priceInput.dispatchEvent(new Event('change', { bubbles: true }));
            await wait(300);

            // Save progress BEFORE clicking save — page may reload on postback
            try {
                var progressState = loadState();
                if (progressState) { progressState.itemIdx = i + 1; saveState(progressState); }
            } catch(e) {}

            saveBtn.scrollIntoView({ block: 'center' });
            saveBtn.click();

            // Wait for the rateinput to disappear — reliable signal that edit mode closed
            try {
                await waitFor(function() {
                    return document.querySelector('input.rateinput') ? null : true;
                }, 10000);
            } catch(e) { /* timed out — proceed anyway */ }

            try { await waitForRows(); } catch(e) { await wait(2500); }
            await wait(600);
            done++;
            log('    Saved', '#a6e3a1');
        }
        return done;
    }

    // ── Main run loop (resumes across page navigations) ───────────────────────

    async function runFromState(st) {
        stopFlag = false;
        var runBtn  = document.getElementById('bb-run');
        var stopBtn = document.getElementById('bb-stop');
        if (runBtn)  runBtn.disabled  = true;
        if (stopBtn) stopBtn.disabled = false;

        var groups = st.groups;
        var gi     = st.groupIdx || 0;
        var group  = groups[gi];

        // Navigate to correct URL if not already there
        if (!currentPageMatchesGroup(group)) {
            var targetUrl = group.url || (WASTE_URLS[group.wasteType] ? WASTE_URLS[group.wasteType] : null);
            if (targetUrl) {
                log('Navigating to ' + group.wasteType + '...', '#89b4fa');
                saveState(st);
                await wait(400);
                window.location.href = targetUrl;
                return;
            }
        }

        // Wait for table to load
        try { await waitForRows(); } catch(e) { log('Table load timed out.', '#f38ba8'); }

        var groups = st.groups;
        var gi     = st.groupIdx || 0;
        var group  = groups[gi];

        log('--- ' + group.wasteType + ' ---', '#cba6f7');
        var done = await updateGroupItems(group.items, st.itemIdx || 0);
        log(group.wasteType + ': ' + done + '/' + group.items.length + ' saved.', '#a6e3a1');

        gi++;
        st.itemIdx = 0;

        if (!stopFlag && gi < groups.length) {
            // Navigate to next waste type — prefer _url from JSON, fall back to WASTE_URLS
            var nextGroup = groups[gi];
            var nextWt    = nextGroup.wasteType;
            var nextUrl   = nextGroup.url || (WASTE_URLS[nextWt] ? WASTE_URLS[nextWt] : null);
            if (!nextUrl) {
                log('No URL for: ' + nextWt + ' - skipping.', '#f38ba8');
                st.groupIdx = gi + 1;
            } else {
                st.groupIdx = gi;
                saveState(st);
                log('Navigating to ' + nextWt + '...', '#89b4fa');
                await wait(600);
                window.location.href = nextUrl;
                return;
            }
        }

        // Done
        clearState();
        if (stopFlag) {
            log('Stopped.', '#fab387');
        } else {
            log('All done!', '#a6e3a1');
        }
        if (runBtn)  runBtn.disabled  = false;
        if (stopBtn) stopBtn.disabled = true;
    }

    // ── Panel ─────────────────────────────────────────────────────────────────

    function buildPanel(resumeState) {
        if (document.getElementById('bb-panel')) return;

        var p = document.createElement('div');
        p.id = 'bb-panel';
        p.style.cssText = 'position:fixed;top:20px;right:20px;width:420px;background:#1e1e2e;color:#cdd6f4;border:1px solid #585b70;border-radius:10px;padding:14px 16px;font-family:sans-serif;font-size:13px;z-index:99999;box-shadow:0 6px 28px rgba(0,0,0,.65);';

        var hdr = document.createElement('div');
        hdr.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;';
        var ttl = document.createElement('b'); ttl.style.color = '#cba6f7'; ttl.textContent = 'Rate Updater';
        var minB = document.createElement('button'); minB.id = 'bb-min'; minB.textContent = '-';
        minB.style.cssText = 'background:none;border:none;color:#cdd6f4;cursor:pointer;font-size:20px;padding:0;line-height:1;';
        hdr.appendChild(ttl); hdr.appendChild(minB); p.appendChild(hdr);

        var body = document.createElement('div'); body.id = 'bb-body';

        if (!resumeState) {
            // ── Input UI ──
            var hint = document.createElement('div');
            hint.style.cssText = 'font-size:11px;color:#a6adc8;margin-bottom:6px;';
            hint.textContent = 'Paste the Update Price JSON, then click Update Prices.';
            body.appendChild(hint);

            var ta = document.createElement('textarea');
            ta.id = 'bb-paste';
            ta.placeholder = 'Paste Update Price table here...';
            ta.style.cssText = 'width:100%;height:120px;box-sizing:border-box;background:#181825;color:#cdd6f4;border:1px solid #585b70;border-radius:6px;padding:8px;font-size:11px;font-family:monospace;resize:vertical;margin-bottom:4px;';
            body.appendChild(ta);

            var preview = document.createElement('div');
            preview.id = 'bb-preview';
            preview.style.cssText = 'font-size:11px;color:#a6adc8;margin-bottom:6px;min-height:16px;';
            body.appendChild(preview);

            ta.addEventListener('input', function() {
                var groups = parsePaste(ta.value);
                if (!groups || !groups.length) { preview.textContent = ''; return; }
                var total = groups.reduce(function(s, g) { return s + g.items.length; }, 0);
                preview.style.color = '#a6e3a1';
                preview.textContent = total + ' price(s) across ' + groups.length + ' waste type(s) ready.';
            });
        }

        // ── Buttons ──
        var btnRow = document.createElement('div'); btnRow.style.cssText = 'display:flex;gap:6px;margin-bottom:6px;';
        var clr  = document.createElement('button'); clr.textContent = resumeState ? 'Cancel' : 'Clear';
        clr.style.cssText = 'flex:1;padding:7px;background:#313244;color:#cdd6f4;border:none;border-radius:5px;cursor:pointer;font-size:12px;';
        var run  = document.createElement('button'); run.id = 'bb-run'; run.textContent = 'Update Prices';
        run.style.cssText = 'flex:2;padding:7px;background:#cba6f7;color:#1e1e2e;border:none;border-radius:5px;cursor:pointer;font-weight:bold;font-size:13px;';
        var stop = document.createElement('button'); stop.id = 'bb-stop'; stop.textContent = 'Stop';
        stop.disabled = true;
        stop.style.cssText = 'flex:1;padding:7px;background:#45475a;color:#cdd6f4;border:none;border-radius:5px;cursor:pointer;font-size:12px;';
        btnRow.appendChild(clr); btnRow.appendChild(run); btnRow.appendChild(stop);
        body.appendChild(btnRow);

        var logDiv = document.createElement('div'); logDiv.id = 'bb-log';
        logDiv.style.cssText = 'max-height:160px;overflow-y:auto;background:#181825;border:1px solid #313244;border-radius:5px;padding:6px;font-size:11px;line-height:1.7;' + (resumeState ? '' : 'display:none;');
        body.appendChild(logDiv);

        // Restore previous logs when resuming
        if (resumeState && resumeState.logs) {
            resumeState.logs.forEach(function(entry) {
                logDiv.innerHTML += '<div style="color:' + entry.color + '">' + entry.msg + '</div>';
            });
            logDiv.scrollTop = logDiv.scrollHeight;
        }

        p.appendChild(body);
        document.body.appendChild(p);

        // ── Events ──
        minB.addEventListener('click', function() {
            body.style.display = body.style.display === 'none' ? '' : 'none';
            minB.textContent   = body.style.display === 'none' ? '+' : '-';
        });

        clr.addEventListener('click', function() {
            clearState();
            stopFlag = true;
            if (!resumeState) {
                var ta2 = document.getElementById('bb-paste');
                if (ta2) ta2.value = '';
                var prev = document.getElementById('bb-preview');
                if (prev) prev.textContent = '';
                logDiv.innerHTML = ''; logDiv.style.display = 'none';
            } else {
                // Rebuild as fresh input panel
                var panel = document.getElementById('bb-panel');
                if (panel) panel.remove();
                buildPanel(null);
            }
        });

        stop.addEventListener('click', function() { stopFlag = true; });

        run.addEventListener('click', function() {
            if (resumeState) { runFromState(resumeState); return; }

            var ta2    = document.getElementById('bb-paste');
            var groups = parsePaste(ta2 ? ta2.value : '');
            if (!groups || !groups.length) {
                logDiv.innerHTML = '<div style="color:#f38ba8">No valid data. Paste the Update Price table first.</div>';
                logDiv.style.display = '';
                return;
            }
            var dateVal  = '';
            var firstGroup = groups[0];
            var firstWt   = firstGroup.wasteType;
            var firstUrl  = firstGroup.url || (WASTE_URLS[firstWt] ? WASTE_URLS[firstWt] : null);
            if (!firstUrl) {
                logDiv.innerHTML = '<div style="color:#f38ba8">No URL mapping for: ' + firstWt + '</div>';
                logDiv.style.display = '';
                return;
            }
            var st = { groups: groups, groupIdx: 0, logs: [] };
            saveState(st);
            logDiv.innerHTML = ''; logDiv.style.display = '';
            log('Starting... navigating to ' + firstWt + '...');
            setTimeout(function() { window.location.href = firstUrl; }, 700);
        });

        // Auto-start when resuming
        if (resumeState) {
            setTimeout(function() { runFromState(resumeState); }, 900);
        }
    }

    // ── Init ──────────────────────────────────────────────────────────────────

    function init() {
        console.log('[RateUpdater] init');
        var state = loadState();
        // If resuming, only run on rates pages; otherwise show input panel on any bookabin page
        if (state && !isRatesPage()) {
            // Navigate to the correct rates page for the current group
            var gi = state.groupIdx || 0;
            var group = state.groups && state.groups[gi];
            var targetUrl = group && (group.url || (WASTE_URLS[group.wasteType] ? WASTE_URLS[group.wasteType] : null));
            if (targetUrl) {
                buildPanel(state);
                setTimeout(function() { window.location.href = targetUrl; }, 600);
                return;
            }
        }
        buildPanel(state || null);
    }

    if (document.readyState === 'loading') {
        window.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
