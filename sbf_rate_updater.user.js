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

    function isEditPage() {
        return /[?&]action=edit/i.test(decodeURIComponent(window.location.href));
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

    // Return the stock value for a given bin size (number string or number).
    // Rules:
    //   2m³         → 30
    //   < 7m³ (other) → 20
    //   7m³, 7.5m³  → 10
    //   > 7.5m³     → null (skip stock)
    function getStockForSize(sz) {
        var n = parseFloat(sz);
        if (isNaN(n)) return null;
        if (n === 2)  return 30;
        if (n < 7)    return 20;
        if (n <= 7.5) return 10;
        return null;
    }

    function fillInput(inp, val) {
        var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        inp.focus();
        inp.select();
        setter.call(inp, String(val));
        inp.dispatchEvent(new Event('input',  { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
    }

    // ── Bin-group helpers ──────────────────────────────────────────────────────

    // Extract the bin size number from any text.
    // Handles formats like:
    //   "2 cubic metres"
    //   "Marrel / 2 cubic metres / No Max Weight"
    //   "Marrel 3 cubic metres"
    //   "2m3", "2 m³"
    function extractSizeFromText(txt) {
        // Primary: number immediately followed by cubic/m3/metres
        var m = txt.match(/\b(\d+(?:\.\d+)?)\s*(?:cubic|m(?:3|³|etres?))\b/i);
        if (m) return m[1];
        // Fallback: "Marrel [/] X" — number that follows the word "Marrel"
        m = txt.match(/\bmarrel\b[^0-9]*(\d+(?:\.\d+)?)/i);
        if (m) return m[1];
        return null;
    }

    /**
     * Detect bin groups from the table on the EDIT page.
     * On the edit page every bin's price row contains:
     *   - The size text ("Marrel / X cubic metres / ...")
     *   - Editable inputs in td[3]–td[17]
     *   - A save <input> element in the last td (td[18])
     * The stock row is immediately after (priceRowIdx + 1).
     */
    function findBinGroups() {
        var rows = getAllTableRows();
        var groups = [];

        for (var i = 0; i < rows.length; i++) {
            var sz = extractSizeFromText(rows[i].innerText);
            if (!sz) continue;

            // On view page: look for edit anchor/img
            var editBtnEl = findEditBtn(rows[i]);

            // On edit page: the last td contains a save input instead of the edit anchor
            var tds = rows[i].querySelectorAll('td');
            var lastTd = tds.length ? tds[tds.length - 1] : null;
            var saveInput = lastTd ? (lastTd.querySelector('input[type="submit"], input[type="image"], input[type="button"]') ||
                lastTd.querySelector('input')) : null;
            // Exclude if it's the edit img (view page)
            if (saveInput && saveInput.tagName === 'IMG') saveInput = null;

            var stockRow = (i + 1 < rows.length) ? rows[i + 1] : null;

            groups.push({
                size: sz,
                priceRow: rows[i],
                priceRowIdx: i,
                stockRow: stockRow,
                editBtnEl: editBtnEl,
                saveInput: saveInput
            });
        }
        return groups;
    }

    // Collect all editable inputs in a row (exclude hidden, readonly, disabled)
    function getEditableInputs(row) {
        var all = row ? row.querySelectorAll('input[type="text"], input[type="number"], input:not([type])') : [];
        return Array.prototype.filter.call(all, function(inp) {
            return !inp.readOnly && !inp.disabled && inp.type !== 'hidden';
        });
    }

    // Build a size→price lookup map from items array
    function buildPriceMap(items) {
        var map = {};
        items.forEach(function(it) { map[it.size] = it.price; });
        return map;
    }

    async function updateAllGroupsOnPage(priceMap) {
        var rows = getAllTableRows();
        log('  [debug] total rows on page: ' + rows.length, '#585b70');

        // Dump ALL rows with input counts so we can diagnose
        rows.forEach(function(r, ri) {
            var txt = r.innerText.replace(/\s+/g, ' ').trim().substring(0, 80);
            var allInp = r.querySelectorAll('input');
            var editableInp = getEditableInputs(r);
            var sz = extractSizeFromText(r.innerText);
            log('  [debug] row[' + ri + '] sz=' + (sz||'?') + ' inputs=' + allInp.length + ' editable=' + editableInp.length + ' "' + txt + '"', '#585b70');
            if (allInp.length) {
                Array.prototype.forEach.call(allInp, function(inp) {
                    log('    input type=' + inp.type + ' name=' + inp.name + ' readOnly=' + inp.readOnly + ' disabled=' + inp.disabled + ' val="' + inp.value + '"', '#313244');
                });
            }
        });

        var groups = findBinGroups();
        if (!groups.length) {
            log('  No bin groups found on page!', '#f38ba8');
            return 0;
        }
        log('  Bin groups found: ' + groups.length + ', priceMap: ' + JSON.stringify(priceMap), '#a6adc8');

        var done = 0;
        var firstSaveBtn = null;
        for (var j = 0; j < groups.length; j++) {
            if (stopFlag) return done;
            var g     = groups[j];
            var price = (priceMap && priceMap[g.size] !== undefined) ? priceMap[g.size] : undefined;
            var stock = getStockForSize(g.size);

            log('  group[' + j + '] size=' + g.size + ' price=' + price + ' stock=' + stock + ' saveInput=' + (g.saveInput ? g.saveInput.outerHTML.substring(0,80) : 'null'), '#a6adc8');

            if (price === undefined && stock === null) {
                log('  ' + g.size + 'm\u00b3: no price or stock rule — skipping.', '#a6adc8');
                continue;
            }

            // Fill price inputs
            if (price !== undefined && price !== null) {
                var priceInputs = getEditableInputs(g.priceRow);
                log('    priceRow editable inputs: ' + priceInputs.length, '#585b70');
                priceInputs.forEach(function(inp) { fillInput(inp, price); });
                log('    price: ' + priceInputs.length + ' cell(s) -> $' + price, '#a6adc8');
            }

            // Fill stock inputs
            if (stock !== null && g.stockRow) {
                var stockInputs = getEditableInputs(g.stockRow);
                log('    stockRow editable inputs: ' + stockInputs.length, '#585b70');
                stockInputs.forEach(function(inp) { fillInput(inp, stock); });
                log('    stock: ' + stockInputs.length + ' cell(s) -> ' + stock, '#a6adc8');
            } else if (stock !== null) {
                log('    stock row not found', '#fab387');
            }

            if (!firstSaveBtn) {
                var saveCandidate = g.saveInput;
                if (!saveCandidate) {
                    var tds = g.priceRow.querySelectorAll('td');
                    var lastTd = tds.length ? tds[tds.length - 1] : null;
                    saveCandidate = lastTd ? lastTd.querySelector('input[type="submit"], input[type="image"], input[type="button"]') : null;
                }
                if (saveCandidate) firstSaveBtn = saveCandidate;
            }

            done++;
        }

        if (!done) return 0;

        var saveBtn = firstSaveBtn
            || document.querySelector('input[type="submit"], button[type="submit"]');
        if (!saveBtn) {
            log('  save button not found — cannot submit', '#f38ba8');
            return 0;
        }
        log('  Submitting: ' + saveBtn.outerHTML.substring(0, 120), '#a6adc8');
        saveBtn.scrollIntoView({ block: 'center' });
        saveBtn.click();
        await wait(1200);
        log('  Submitted \u2713 (' + done + ' bin(s))', '#a6e3a1');
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

        // ── Derive edit URL from view URL ─────────────────────────────────────
        // view URL:  rates_manage.php?min_date=YYYY-MM-DD
        // edit URL:  rates_manage.php?action=edit&min_date=YYYY-MM-DD
        function toEditUrl(url) {
            if (!url) return null;
            if (/[?&]action=edit/i.test(url)) return url;
            return url.indexOf('?') !== -1
                ? url.replace('?', '?action=edit&')
                : url + '?action=edit';
        }

        var viewUrl = group.url || (WASTE_URLS[group.wasteType] ? WASTE_URLS[group.wasteType] : null);
        var editUrl = toEditUrl(viewUrl);

        // ── Navigate to edit page if not already there ────────────────────────
        if (!isEditPage() || !currentPageMatchesGroup(group)) {
            if (editUrl) {
                log('Navigating to edit page for ' + group.wasteType + '...', '#89b4fa');
                saveState(st);
                await wait(400);
                window.location.href = editUrl;
                return;
            }
        }

        // ── We are on the edit page ───────────────────────────────────────────
        try { await waitForRows(); } catch(e) { log('Table load timed out.', '#f38ba8'); }
        log('[debug] href=' + decodeURIComponent(window.location.href), '#585b70');

        log('--- ' + group.wasteType + ' ---', '#cba6f7');
        var priceMap = buildPriceMap(group.items);
        var done = await updateAllGroupsOnPage(priceMap);
        log(group.wasteType + ': ' + done + ' bin(s) updated.', '#a6e3a1');

        gi++;
        if (!stopFlag && gi < groups.length) {
            var nextGroup = groups[gi];
            var nextWt    = nextGroup.wasteType;
            var nextEditUrl = toEditUrl(nextGroup.url || (WASTE_URLS[nextWt] ? WASTE_URLS[nextWt] : null));
            if (!nextEditUrl) {
                log('No URL for: ' + nextWt + ' - skipping.', '#f38ba8');
                st.groupIdx = gi + 1;
                saveState(st);
                await runFromState(st);
                return;
            }
            st.groupIdx = gi;
            saveState(st);
            log('Navigating to ' + nextWt + '...', '#89b4fa');
            await wait(600);
            window.location.href = nextEditUrl;
            return;
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

            // Stock rules display
            var stockInfo = document.createElement('div');
            stockInfo.style.cssText = 'font-size:10px;color:#a6adc8;margin-bottom:6px;background:#181825;border:1px solid #313244;border-radius:4px;padding:5px 8px;line-height:1.7;';
            stockInfo.innerHTML = '<b style="color:#89dceb">Stock rules:</b> 2m³ → 30 &nbsp;|  3–6m³ → 20 &nbsp;|  7–7.5m³ → 10 &nbsp;|  >7.5m³ → skip';
            body.appendChild(stockInfo);
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

        if (isRatesPage()) {
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
        logDiv.style.cssText = 'max-height:300px;overflow-y:auto;background:#181825;border:1px solid #313244;border-radius:5px;padding:6px;font-size:10px;line-height:1.6;' + (resumeState ? '' : 'display:none;');
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
        var href = decodeURIComponent(window.location.href);
        console.log('[SBF RateUpdater] init href=' + href);
        var state = loadState();
        if (state) {
            // Resume on both view pages and edit pages
            if (isRatesPage()) {
                buildPanel(state);
                return;
            }
            // On a non-rates page, navigate to the edit page for current group
            var gi = state.groupIdx || 0;
            var group = state.groups && state.groups[gi];
            var baseUrl = group && (group.url || (WASTE_URLS[group.wasteType] ? WASTE_URLS[group.wasteType] : null));
            if (baseUrl) {
                var editUrl = baseUrl.indexOf('?') !== -1
                    ? baseUrl.replace('?', '?action=edit&')
                    : baseUrl + '?action=edit';
                buildPanel(state);
                setTimeout(function() { window.location.href = editUrl; }, 600);
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
