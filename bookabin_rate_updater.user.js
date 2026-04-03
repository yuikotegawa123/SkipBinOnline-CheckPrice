// ==UserScript==
// @name         BookABin Rate Updater
// @namespace    bookabin-rate-updater
// @version      6.0
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
        var tbody = document.querySelector('table#dltRates tbody');
        if (!tbody) return [];
        return Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    }

    function waitForRows() {
        return waitFor(function() {
            var tbody = document.querySelector('table#dltRates tbody');
            return (tbody && tbody.querySelectorAll('tr').length > 0) ? true : null;
        }, 15000);
    }

    // Find the row that contains the Edit button for a given size.
    // BookABin uses row pairs: one row has size/price text, the adjacent has the Edit button (or same row).
    function findRowForSize(sz) {
        var szPat = new RegExp('(?<![\\d.])' + sz.toString().replace('.', '\\.') + '(?![\\d.])', 'i');
        var rows = getAllTableRows();
        for (var i = 0; i < rows.length; i++) {
            var txt = rows[i].innerText.replace(/\s+/g, ' ');
            if (!szPat.test(txt)) continue;
            // Check this row for Edit button first
            var btn = rows[i].querySelector('input[alt="Edit Row"], input[title="Edit Row"]');
            if (btn) return rows[i];
            // Check adjacent rows (prev/next in pair)
            if (i > 0) {
                btn = rows[i - 1].querySelector('input[alt="Edit Row"], input[title="Edit Row"]');
                if (btn) return rows[i - 1];
            }
            if (i < rows.length - 1) {
                btn = rows[i + 1].querySelector('input[alt="Edit Row"], input[title="Edit Row"]');
                if (btn) return rows[i + 1];
            }
        }
        return null;
    }

    // ── Paste Parser ─────────────────────────────────────────────────────────
    // Expects TSV from Streamlit Copy to Clipboard:
    //   Waste Type\t2 m3\t3 m3\t...
    //   General Waste\t$208\t$298\t...

    function parsePaste(text) {
        var lines = text.trim().split(/\r?\n/);
        if (lines.length < 2) return null;
        var headers = lines[0].split('\t');
        var sizes = headers.slice(1).map(function(h) {
            return h.replace(/\s*m.*/i, '').trim();
        });
        var map = {}, order = [];
        for (var i = 1; i < lines.length; i++) {
            var cols = lines[i].split('\t');
            var wt = cols[0].trim();
            if (!wt) continue;
            if (!map[wt]) { map[wt] = []; order.push(wt); }
            for (var j = 0; j < sizes.length; j++) {
                var cell = (cols[j + 1] || '').trim();
                if (!cell || cell === 'N/A' || cell === '-') continue;
                var pr = parseInt(cell.replace(/[$,]/g, ''), 10);
                if (!isNaN(pr)) map[wt].push({ size: sizes[j], price: pr });
            }
        }
        if (!order.length) return null;
        return order.map(function(wt) { return { wasteType: wt, items: map[wt] }; });
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

    async function updateGroupItems(items) {
        var done = 0;
        for (var i = 0; i < items.length; i++) {
            if (stopFlag) return done;
            var sz = items[i].size, price = items[i].price;

            var row = findRowForSize(sz);
            if (!row) { log('  ' + sz + 'm3: row not found - skipping.', '#f38ba8'); continue; }

            var editBtn = row.querySelector('input[alt="Edit Row"], input[title="Edit Row"]');
            if (!editBtn) { log('  ' + sz + 'm3: Edit button not found - skipping.', '#f38ba8'); continue; }

            log('  ' + sz + 'm3 -> $' + price);
            editBtn.scrollIntoView({ block: 'center', behavior: 'smooth' });
            await wait(400);
            editBtn.click();

            var editTr;
            try {
                editTr = await waitFor(function() {
                    var b = document.querySelector('input[alt="Update Row"], input[title="Update Row"]');
                    return b ? b.closest('tr') : null;
                }, 8000);
            } catch(e) {
                log('  ' + sz + 'm3: timed out - skipping.', '#f38ba8');
                var cc = document.querySelector('input[alt="Cancel"], input[title="Cancel"]');
                if (cc) cc.click();
                continue;
            }

            var priceInput = document.evaluate(
                './/td[@class="ratecelledit"][4]/input',
                editTr, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
            ).singleNodeValue;

            if (!priceInput) {
                log('  ' + sz + 'm3: price input not found - cancelling.', '#f38ba8');
                var cn = editTr.querySelector('input[alt="Cancel"], input[title="Cancel"]');
                if (cn) cn.click();
                continue;
            }

            priceInput.focus();
            priceInput.select();
            var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(priceInput, String(price));
            priceInput.dispatchEvent(new Event('input',  { bubbles: true }));
            priceInput.dispatchEvent(new Event('change', { bubbles: true }));
            await wait(300);

            var updateBtn = editTr.querySelector('input[alt="Update Row"], input[title="Update Row"]');
            if (!updateBtn) { log('  ' + sz + 'm3: Update Row button missing.', '#f38ba8'); continue; }

            updateBtn.scrollIntoView({ block: 'center' });
            updateBtn.click();

            try { await waitForRows(); } catch(e) { await wait(2500); }
            await wait(400);
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

        // Wait for table to load
        try { await waitForRows(); } catch(e) { log('Table load timed out.', '#f38ba8'); }

        var groups = st.groups;
        var gi     = st.groupIdx || 0;
        var group  = groups[gi];

        log('--- ' + group.wasteType + ' ---', '#cba6f7');
        var done = await updateGroupItems(group.items);
        log(group.wasteType + ': ' + done + '/' + group.items.length + ' saved.', '#a6e3a1');

        gi++;

        if (!stopFlag && gi < groups.length) {
            // Navigate to next waste type
            var nextWt   = groups[gi].wasteType;
            var nextBase = WASTE_URLS[nextWt];
            if (!nextBase) {
                log('No URL for: ' + nextWt + ' - skipping.', '#f38ba8');
                st.groupIdx = gi + 1;
            } else {
                var nextUrl = nextBase + (st.date ? '?fromdate=' + encodeURIComponent(st.date) : '');
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
            hint.textContent = 'Enter delivery date, paste the Update Price table, then click Update Prices.';
            body.appendChild(hint);

            var dateRow = document.createElement('div');
            dateRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:6px;';
            var dateLbl = document.createElement('label');
            dateLbl.textContent = 'Delivery Date:';
            dateLbl.style.cssText = 'font-size:11px;color:#a6adc8;white-space:nowrap;';
            var dateInp = document.createElement('input');
            dateInp.type = 'text'; dateInp.id = 'bb-date'; dateInp.placeholder = 'D/MM/YYYY';
            dateInp.style.cssText = 'flex:1;background:#181825;color:#cdd6f4;border:1px solid #585b70;border-radius:4px;padding:4px 8px;font-size:12px;';
            dateRow.appendChild(dateLbl); dateRow.appendChild(dateInp);
            body.appendChild(dateRow);

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
            var dateVal  = (document.getElementById('bb-date') || {}).value || '';
            var firstWt  = groups[0].wasteType;
            var firstBase = WASTE_URLS[firstWt];
            if (!firstBase) {
                logDiv.innerHTML = '<div style="color:#f38ba8">No URL mapping for: ' + firstWt + '</div>';
                logDiv.style.display = '';
                return;
            }
            var st = { date: dateVal, groups: groups, groupIdx: 0, logs: [] };
            saveState(st);
            var firstUrl = firstBase + (dateVal ? '?fromdate=' + encodeURIComponent(dateVal) : '');
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
        if (!isRatesPage()) return;
        console.log('[RateUpdater] init');
        var state = loadState();
        buildPanel(state || null);
    }

    if (document.readyState === 'loading') {
        window.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
