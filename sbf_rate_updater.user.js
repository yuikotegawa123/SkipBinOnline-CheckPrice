// ==UserScript==
// @name         SkipBinFinder Rate Updater
// @namespace    sbf-rate-updater
// @version      1.0
// @description  Auto-navigates per waste type using pasted Update Price JSON from the tool
// @match        *://*.skipbinfinder.com.au/*
// @run-at       document-end
// @grant        none
// ==/UserScript==

(function () {

    var WASTE_URLS = {
        'General Waste':                            'https://www.skipbinfinder.com.au/supplier/rates_manage.php',
        'Mixed Heavy Waste':                        'https://www.skipbinfinder.com.au/supplier/rates_manage_mixedheavy.php',
        'Mixed Heavy Waste (With no Soild & Dirt)': 'https://www.skipbinfinder.com.au/supplier/rates_manage_mixedheavynosoildirt.php',
        'Concrete / Bricks':                        'https://www.skipbinfinder.com.au/supplier/rates_manage_clean.php',
        'Green Garden Waste':                       'https://www.skipbinfinder.com.au/supplier/rates_manage_green.php',
        'Soil / Dirt':                              'https://www.skipbinfinder.com.au/supplier/rates_manage_dirt.php',
    };

    var STATE_KEY = 'sbf_updater_state';
    var stopFlag  = false;

    // ── Helpers ──────────────────────────────────────────────────────────────

    function isRatesPage() {
        return /rates_manage/i.test(decodeURIComponent(window.location.href));
    }

    function currentPageMatchesGroup(group) {
        var url = group.url || (WASTE_URLS[group.wasteType] ? WASTE_URLS[group.wasteType] : null);
        if (!url) return true;
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

    // ── Table row helpers ─────────────────────────────────────────────────────

    function getAllTableRows() {
        // Try a rates table first, then fall back to all rows
        var candidates = document.querySelectorAll('table');
        for (var t = 0; t < candidates.length; t++) {
            var id  = (candidates[t].id   || '').toLowerCase();
            var cls = (candidates[t].className || '').toLowerCase();
            if (id.indexOf('rate') !== -1 || cls.indexOf('rate') !== -1) {
                return Array.prototype.slice.call(candidates[t].querySelectorAll('tr'));
            }
        }
        return Array.prototype.slice.call(document.querySelectorAll('tr'));
    }

    function waitForRows() {
        return waitFor(function() {
            return getAllTableRows().length > 0 ? true : null;
        }, 15000);
    }

    // Find the edit icon/button in a row (pencil icon, edit link, image button)
    function findEditBtn(row) {
        // Prefer image buttons or anchors with edit-related src/alt/title/text
        return row.querySelector(
            'img[src*="edit" i], img[alt*="edit" i], ' +
            'a > img[src*="edit" i], a > img[alt*="edit" i], ' +
            'input[type="image"][src*="edit" i], input[type="image"][alt*="edit" i], ' +
            'a[title*="edit" i], a[href*="edit" i], a[onclick*="edit" i], ' +
            'button[title*="edit" i], button[onclick*="edit" i], ' +
            'input[type="button"][value*="edit" i], input[type="submit"][value*="edit" i]'
        ) || (function() {
            // Fallback: anchor/button whose trimmed text is just a pencil symbol or "Edit"
            var all = row.querySelectorAll('a, button');
            for (var i = 0; i < all.length; i++) {
                var t = all[i].textContent.trim();
                if (/^(edit|✏|🖊|✎)$/i.test(t)) return all[i];
            }
            return null;
        })();
    }

    // Find the save icon/button in a row after entering edit mode
    function findSaveBtn(row) {
        return row.querySelector(
            'img[src*="save" i], img[alt*="save" i], img[src*="tick" i], img[alt*="tick" i], ' +
            'img[src*="ok" i], img[alt*="ok" i], img[src*="check" i], img[alt*="check" i], ' +
            'input[type="image"][src*="save" i], input[type="image"][alt*="save" i], ' +
            'a[title*="save" i], a[onclick*="save" i], a[title*="update" i], ' +
            'button[title*="save" i], button[title*="update" i], ' +
            'input[type="button"][value*="save" i], input[type="submit"][value*="save" i], ' +
            'input[type="button"][value*="update" i], input[type="submit"][value*="update" i]'
        ) || (function() {
            var all = row.querySelectorAll('a, button');
            for (var i = 0; i < all.length; i++) {
                var t = all[i].textContent.trim();
                if (/^(save|update|✓|✔|💾)$/i.test(t)) return all[i];
            }
            return null;
        })();
    }

    // Find a save/update button anywhere on the page (e.g. in a modal/edit form)
    function findSaveBtnPage() {
        return document.querySelector(
            'input[type="image"][src*="save" i], input[type="image"][alt*="save" i], ' +
            'input[type="image"][src*="tick" i], input[type="image"][alt*="tick" i], ' +
            'input[type="submit"][name*="save" i], input[type="submit"][value*="save" i], ' +
            'input[type="submit"][name*="update" i], input[type="submit"][value*="update" i], ' +
            'button[type="submit"][name*="save" i], button[type="submit"][value*="save" i], ' +
            'input[type="button"][value*="save" i], input[type="button"][value*="update" i]'
        );
    }

    // Find price input in an edit row or edit form
    function findPriceInput(context) {
        return context.querySelector(
            'input[name*="price" i], input[id*="price" i], ' +
            'input[name*="rate" i], input[id*="rate" i], ' +
            'input[type="text"][name*="cost" i], input[type="number"][name*="price" i], ' +
            'input[type="number"][name*="rate" i]'
        ) || context.querySelector('input[type="number"], input[type="text"]');
    }

    // Find a stock/quantity input in an edit row or form
    function findStockInput(context, skipEl) {
        var candidates = context.querySelectorAll(
            'input[name*="stock" i], input[id*="stock" i], ' +
            'input[name*="qty" i], input[id*="qty" i], ' +
            'input[name*="quantity" i], input[id*="quantity" i], ' +
            'input[name*="avail" i], input[id*="avail" i]'
        );
        for (var i = 0; i < candidates.length; i++) {
            if (candidates[i] !== skipEl) return candidates[i];
        }
        // Fallback: second text/number input that isn't the price input
        var all = context.querySelectorAll('input[type="text"], input[type="number"]');
        for (var j = 0; j < all.length; j++) {
            if (all[j] !== skipEl) return all[j];
        }
        return null;
    }

    // Find the row for a given size
    function findRowForSize(sz) {
        var numStr = sz.toString().replace(/\s*m(?:3|³|etres?)?.*$/i, '').trim();
        var szPat = new RegExp('(?<![\\d.])' + numStr.replace('.', '\\.') + '(?![\\d.])', 'i');
        var rows = getAllTableRows();

        if (rows.length === 0) {
            log('  [debug] No table rows found on page!', '#fab387');
            return null;
        }

        for (var i = 0; i < rows.length; i++) {
            var txt = rows[i].innerText.replace(/\s+/g, ' ').trim();
            if (!szPat.test(txt)) continue;
            // Avoid header rows — must have an edit control or price-like content
            var limit = Math.min(rows.length - 1, i + 3);
            for (var j = i; j <= limit; j++) {
                var btn = findEditBtn(rows[j]);
                if (btn) return rows[j];
            }
            // Return matching row for caller even if no edit btn found yet
            return rows[i];
        }

        var sample = rows.slice(0, Math.min(5, rows.length)).map(function(r) {
            return '"' + r.innerText.replace(/\s+/g, ' ').trim().substring(0, 80) + '"';
        }).join(' | ');
        log('  [debug] sz=' + sz + ' not found. Sample rows: ' + sample, '#fab387');
        return null;
    }

    // ── JSON Parser ───────────────────────────────────────────────────────────

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
        var el = document.getElementById('sbf-log');
        if (el) {
            el.innerHTML += '<div style="color:' + (color || '#f9e2af') + '">' + msg + '</div>';
            el.scrollTop = el.scrollHeight;
        }
        console.log('[SBF RateUpdater] ' + msg);
        try {
            var st = loadState();
            if (st) { st.logs = st.logs || []; st.logs.push({ msg: msg, color: color || '#f9e2af' }); saveState(st); }
        } catch(e) {}
    }

    // ── Update one group's items ──────────────────────────────────────────────

    function getDefaultStock() {
        var el = document.getElementById('sbf-stock');
        var v = el ? parseInt(el.value, 10) : NaN;
        return isNaN(v) ? null : v;
    }

    function fillInput(inp, val) {
        var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        inp.focus();
        inp.select();
        setter.call(inp, String(val));
        inp.dispatchEvent(new Event('input',  { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
    }

    async function updateGroupItems(items, startIdx) {
        var done = 0;
        for (var i = (startIdx || 0); i < items.length; i++) {
            if (stopFlag) return done;
            var sz = items[i].size, price = items[i].price;

            var row = findRowForSize(sz);
            if (!row) { log('  ' + sz + 'm³: row not found - skipping.', '#f38ba8'); continue; }

            var editBtn = findEditBtn(row);
            if (!editBtn) {
                log('  ' + sz + 'm³: Edit icon not found - skipping. Row: ' + row.innerText.substring(0, 100), '#f38ba8');
                continue;
            }

            log('  ' + sz + 'm³ -> $' + price);
            editBtn.scrollIntoView({ block: 'center', behavior: 'smooth' });
            await wait(400);
            // If edit btn is an img inside an anchor, click the anchor
            var clickTarget = editBtn;
            if (editBtn.tagName === 'IMG' && editBtn.parentElement && editBtn.parentElement.tagName === 'A') {
                clickTarget = editBtn.parentElement;
            }
            clickTarget.click();
            await wait(800);

            // After clicking edit, look for save icon in the same row first
            var editRow = row;
            var saveBtn = findSaveBtn(editRow);
            if (!saveBtn) {
                // Inline edit may expand a sibling row — check next 2 rows in DOM
                var trs = getAllTableRows();
                var rowIdx = trs.indexOf(editRow);
                for (var ri = rowIdx + 1; ri <= rowIdx + 2 && ri < trs.length; ri++) {
                    saveBtn = findSaveBtn(trs[ri]);
                    if (saveBtn) { editRow = trs[ri]; break; }
                }
            }
            if (!saveBtn) {
                // Fall back to page-wide search (modal / separate form)
                try {
                    saveBtn = await waitFor(function() { return findSaveBtnPage() || null; }, 6000);
                } catch(e) {
                    log('  ' + sz + 'm³: timed out waiting for save icon - skipping.', '#f38ba8');
                    continue;
                }
            }

            // Find price input — prefer edit row context, then save button's form, then page
            var ctx = saveBtn.closest ? (saveBtn.closest('tr') || saveBtn.closest('form') || document) : document;
            var priceInput = findPriceInput(ctx);
            if (!priceInput) priceInput = findPriceInput(document);

            if (!priceInput) {
                log('  ' + sz + 'm³: price input not found - skipping.', '#f38ba8');
                var cancelBtn = document.querySelector(
                    'input[type="image"][src*="cancel" i], input[type="image"][alt*="cancel" i], ' +
                    'a[href*="cancel" i], a[onclick*="cancel" i], input[value*="cancel" i], button[value*="cancel" i]'
                );
                if (cancelBtn) cancelBtn.click();
                continue;
            }

            fillInput(priceInput, price);
            await wait(200);

            // Fill stock input if a default stock value is set
            var stockVal = getDefaultStock();
            if (stockVal !== null) {
                var stockInput = findStockInput(ctx, priceInput);
                if (!stockInput) stockInput = findStockInput(document, priceInput);
                if (stockInput) {
                    fillInput(stockInput, stockVal);
                    log('    stock -> ' + stockVal, '#a6adc8');
                    await wait(200);
                }
            }

            // Save progress before clicking save (page may reload)
            try {
                var progressState = loadState();
                if (progressState) { progressState.itemIdx = i + 1; saveState(progressState); }
            } catch(e) {}

            saveBtn.scrollIntoView({ block: 'center' });
            // If save btn is an img inside an anchor, click the anchor
            var saveClickTarget = saveBtn;
            if (saveBtn.tagName === 'IMG' && saveBtn.parentElement && saveBtn.parentElement.tagName === 'A') {
                saveClickTarget = saveBtn.parentElement;
            }
            saveClickTarget.click();

            // Wait for page to settle (save icon disappears / rows reload)
            try {
                await waitFor(function() {
                    return !findSaveBtnPage() ? true : null;
                }, 8000);
            } catch(e) { /* proceed anyway */ }

            try { await waitForRows(); } catch(e) { await wait(2000); }
            await wait(600);
            done++;
            log('    Saved ✓', '#a6e3a1');
        }
        return done;
    }

    // ── Main run loop ─────────────────────────────────────────────────────────

    async function runFromState(st) {
        stopFlag = false;
        var runBtn  = document.getElementById('sbf-run');
        var stopBtn = document.getElementById('sbf-stop');
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

        log('--- ' + group.wasteType + ' ---', '#cba6f7');
        var done = await updateGroupItems(group.items, st.itemIdx || 0);
        log(group.wasteType + ': ' + done + '/' + group.items.length + ' saved.', '#a6e3a1');

        gi++;
        st.itemIdx = 0;

        if (!stopFlag && gi < groups.length) {
            var nextGroup = groups[gi];
            var nextWt    = nextGroup.wasteType;
            var nextUrl   = nextGroup.url || (WASTE_URLS[nextWt] ? WASTE_URLS[nextWt] : null);
            if (!nextUrl) {
                log('No URL for: ' + nextWt + ' - skipping.', '#f38ba8');
                st.groupIdx = gi + 1;
                saveState(st);
                await runFromState(st);
                return;
            } else {
                st.groupIdx = gi;
                saveState(st);
                log('Navigating to ' + nextWt + '...', '#89b4fa');
                await wait(600);
                window.location.href = nextUrl;
                return;
            }
        }

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
        if (document.getElementById('sbf-panel')) return;

        var p = document.createElement('div');
        p.id = 'sbf-panel';
        p.style.cssText = 'position:fixed;top:20px;right:20px;width:420px;background:#1e1e2e;color:#cdd6f4;border:1px solid #585b70;border-radius:10px;padding:14px 16px;font-family:sans-serif;font-size:13px;z-index:99999;box-shadow:0 6px 28px rgba(0,0,0,.65);';

        var hdr = document.createElement('div');
        hdr.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;';
        var ttl = document.createElement('b'); ttl.style.color = '#89dceb'; ttl.textContent = 'SBF Rate Updater';
        var minB = document.createElement('button'); minB.id = 'sbf-min'; minB.textContent = '-';
        minB.style.cssText = 'background:none;border:none;color:#cdd6f4;cursor:pointer;font-size:20px;padding:0;line-height:1;';
        hdr.appendChild(ttl); hdr.appendChild(minB); p.appendChild(hdr);

        var body = document.createElement('div'); body.id = 'sbf-body';

        if (!resumeState) {
            var hint = document.createElement('div');
            hint.style.cssText = 'font-size:11px;color:#a6adc8;margin-bottom:6px;';
            hint.textContent = 'Paste the Update Price JSON from the tool, then click Update Prices.';
            body.appendChild(hint);

            var ta = document.createElement('textarea');
            ta.id = 'sbf-paste';
            ta.placeholder = 'Paste Update Price JSON here...';
            ta.style.cssText = 'width:100%;height:120px;box-sizing:border-box;background:#181825;color:#cdd6f4;border:1px solid #585b70;border-radius:6px;padding:8px;font-size:11px;font-family:monospace;resize:vertical;margin-bottom:4px;';
            body.appendChild(ta);

            var preview = document.createElement('div');
            preview.id = 'sbf-preview';
            preview.style.cssText = 'font-size:11px;color:#a6adc8;margin-bottom:6px;min-height:16px;';
            body.appendChild(preview);

            ta.addEventListener('input', function() {
                var groups = parsePaste(ta.value);
                if (!groups || !groups.length) { preview.textContent = ''; return; }
                var total = groups.reduce(function(s, g) { return s + g.items.length; }, 0);
                preview.style.color = '#a6e3a1';
                preview.textContent = total + ' price(s) across ' + groups.length + ' waste type(s) ready.';
            });

            // Default stock input
            var stockRow = document.createElement('div');
            stockRow.style.cssText = 'display:flex;align-items:center;gap:6px;margin-bottom:6px;font-size:11px;color:#a6adc8;';
            var stockLbl = document.createElement('label'); stockLbl.textContent = 'Default Stock:';
            stockLbl.style.cssText = 'white-space:nowrap;';
            var stockInp = document.createElement('input'); stockInp.id = 'sbf-stock'; stockInp.type = 'number';
            stockInp.min = '0'; stockInp.placeholder = '(leave blank to skip)';
            stockInp.style.cssText = 'flex:1;background:#181825;color:#cdd6f4;border:1px solid #585b70;border-radius:4px;padding:4px 6px;font-size:11px;';
            stockRow.appendChild(stockLbl); stockRow.appendChild(stockInp);
            body.appendChild(stockRow);
        }

        var btnRow = document.createElement('div'); btnRow.style.cssText = 'display:flex;gap:6px;margin-bottom:6px;';
        var clr  = document.createElement('button'); clr.textContent = resumeState ? 'Cancel' : 'Clear';
        clr.style.cssText = 'flex:1;padding:7px;background:#313244;color:#cdd6f4;border:none;border-radius:5px;cursor:pointer;font-size:12px;';
        var run  = document.createElement('button'); run.id = 'sbf-run'; run.textContent = 'Update Prices';
        run.style.cssText = 'flex:2;padding:7px;background:#89dceb;color:#1e1e2e;border:none;border-radius:5px;cursor:pointer;font-weight:bold;font-size:13px;';
        var stop = document.createElement('button'); stop.id = 'sbf-stop'; stop.textContent = 'Stop';
        stop.disabled = true;
        stop.style.cssText = 'flex:1;padding:7px;background:#45475a;color:#cdd6f4;border:none;border-radius:5px;cursor:pointer;font-size:12px;';
        btnRow.appendChild(clr); btnRow.appendChild(run); btnRow.appendChild(stop);
        body.appendChild(btnRow);

        if (!resumeState && isRatesPage()) {
            var probeRow = document.createElement('div'); probeRow.style.cssText = 'display:flex;gap:6px;margin-bottom:6px;';
            var probe = document.createElement('button'); probe.textContent = 'Probe DOM';
            probe.style.cssText = 'flex:1;padding:6px;background:#45475a;color:#cdd6f4;border:none;border-radius:5px;cursor:pointer;font-size:11px;';
            probeRow.appendChild(probe);
            body.appendChild(probeRow);
            probe.addEventListener('click', function() {
                var logDiv2 = document.getElementById('sbf-log');
                if (logDiv2) logDiv2.style.display = '';
                // Tables
                var tables = document.querySelectorAll('table');
                log('=== PROBE ===', '#cba6f7');
                log('Tables found: ' + tables.length, '#89b4fa');
                tables.forEach(function(t, ti) {
                    log('  table[' + ti + '] id="' + (t.id||'') + '" class="' + (t.className||'') + '" rows=' + t.rows.length, '#a6adc8');
                });
                // First 5 rows of each table
                tables.forEach(function(t, ti) {
                    var rows = t.querySelectorAll('tr');
                    var limit = Math.min(rows.length, 5);
                    for (var ri = 0; ri < limit; ri++) {
                        var txt = rows[ri].innerText.replace(/\s+/g, ' ').substring(0, 120);
                        var btns = rows[ri].querySelectorAll('a,button,input[type="image"],input[type="submit"],input[type="button"]');
                        log('  t[' + ti + ']r[' + ri + ']: "' + txt + '"', '#f9e2af');
                        btns.forEach(function(b) {
                            log('    btn: ' + b.outerHTML.substring(0, 200), '#fab387');
                        });
                    }
                });
                // All inputs
                var inputs = document.querySelectorAll('input[type="text"],input[type="number"]');
                log('Text/number inputs: ' + inputs.length, '#89b4fa');
                inputs.forEach(function(inp) {
                    log('  ' + inp.outerHTML.substring(0, 200), '#a6e3a1');
                });
                // All submit/image inputs
                var submits = document.querySelectorAll('input[type="submit"],input[type="image"],button[type="submit"]');
                log('Submit/image inputs: ' + submits.length, '#89b4fa');
                submits.forEach(function(s) {
                    log('  ' + s.outerHTML.substring(0, 200), '#fab387');
                });
                log('=== END PROBE ===', '#cba6f7');
            });
        }

        var logDiv = document.createElement('div'); logDiv.id = 'sbf-log';
        logDiv.style.cssText = 'max-height:160px;overflow-y:auto;background:#181825;border:1px solid #313244;border-radius:5px;padding:6px;font-size:11px;line-height:1.7;' + (resumeState ? '' : 'display:none;');
        body.appendChild(logDiv);

        if (resumeState && resumeState.logs) {
            resumeState.logs.forEach(function(entry) {
                logDiv.innerHTML += '<div style="color:' + entry.color + '">' + entry.msg + '</div>';
            });
            logDiv.scrollTop = logDiv.scrollHeight;
        }

        p.appendChild(body);
        document.body.appendChild(p);

        minB.addEventListener('click', function() {
            body.style.display = body.style.display === 'none' ? '' : 'none';
            minB.textContent   = body.style.display === 'none' ? '+' : '-';
        });

        clr.addEventListener('click', function() {
            clearState();
            stopFlag = true;
            if (!resumeState) {
                var ta2 = document.getElementById('sbf-paste');
                if (ta2) ta2.value = '';
                var prev = document.getElementById('sbf-preview');
                if (prev) prev.textContent = '';
                logDiv.innerHTML = ''; logDiv.style.display = 'none';
            } else {
                var panel = document.getElementById('sbf-panel');
                if (panel) panel.remove();
                buildPanel(null);
            }
        });

        stop.addEventListener('click', function() { stopFlag = true; });

        run.addEventListener('click', function() {
            if (resumeState) { runFromState(resumeState); return; }

            var ta2    = document.getElementById('sbf-paste');
            var groups = parsePaste(ta2 ? ta2.value : '');
            if (!groups || !groups.length) {
                logDiv.innerHTML = '<div style="color:#f38ba8">No valid data. Paste the Update Price JSON first.</div>';
                logDiv.style.display = '';
                return;
            }
            var firstGroup = groups[0];
            var firstWt    = firstGroup.wasteType;
            var firstUrl   = firstGroup.url || (WASTE_URLS[firstWt] ? WASTE_URLS[firstWt] : null);
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

        if (resumeState) {
            setTimeout(function() { runFromState(resumeState); }, 900);
        }
    }

    // ── Init ──────────────────────────────────────────────────────────────────

    function init() {
        console.log('[SBF RateUpdater] init');
        var state = loadState();
        if (state && !isRatesPage()) {
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
