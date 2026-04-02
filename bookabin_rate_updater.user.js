// ==UserScript==
// @name         BookABin Rate Updater
// @namespace    bookabin-rate-updater
// @version      1.3
// @description  Auto-fill rates on BookABin rates management page
// @match        *://*.bookabin.com.au/*
// @run-at       document-end
// @grant        none
// ==/UserScript==

(function () {
    if (window.location.href.toLowerCase().indexOf('rates_manage') === -1) return;
    console.log('[RateUpdater] active on: ' + window.location.href);

    var SESSION_KEY = 'bb_rate_queue';

    function parseTable(text) {
        var lines = text.trim().split('\n').filter(function(l) { return l.trim(); });
        if (lines.length < 2) return null;
        var headers = lines[0].split('\t').map(function(h) { return h.trim().toLowerCase(); });
        var sizeIdx = -1, priceIdx = -1;
        for (var h = 0; h < headers.length; h++) {
            if (headers[h].indexOf('bin size') !== -1) sizeIdx = h;
            if (headers[h].indexOf('will set to') !== -1) priceIdx = h;
        }
        if (sizeIdx === -1 || priceIdx === -1) return null;
        var map = {};
        for (var i = 1; i < lines.length; i++) {
            var cols = lines[i].split('\t');
            if (cols.length <= Math.max(sizeIdx, priceIdx)) continue;
            var sz = cols[sizeIdx].trim().replace(/\s*m.*/i, '').trim();
            var pr = parseInt(cols[priceIdx].replace(/[$,]/g, '').trim(), 10);
            if (sz && !isNaN(pr)) map[sz] = pr;
        }
        return Object.keys(map).length ? map : null;
    }

    function findPriceRowForSize(sizeKey) {
        var target = (sizeKey + ' cubic metres').toLowerCase();
        var tds = document.querySelectorAll('td');
        for (var i = 0; i < tds.length; i++) {
            if (tds[i].textContent.trim().toLowerCase() !== target) continue;
            var tr = tds[i].closest('tr');
            for (var j = 0; j < 4; j++) {
                if (!tr) break;
                if (tr.querySelector('input[alt="Edit Row"]')) return tr;
                tr = tr.nextElementSibling;
            }
        }
        return null;
    }

    function waitFor(fn, timeoutMs) {
        return new Promise(function(resolve, reject) {
            var deadline = Date.now() + (timeoutMs || 10000);
            (function check() {
                var r = fn();
                if (r) return resolve(r);
                if (Date.now() > deadline) return reject(new Error('timeout'));
                setTimeout(check, 300);
            })();
        });
    }

    function setLog(msg) {
        var el = document.getElementById('bb-log');
        if (el) el.textContent = msg;
        console.log('[RateUpdater] ' + msg);
    }

    function setRunBtn(disabled) {
        var btn = document.getElementById('bb-run');
        if (btn) btn.disabled = disabled;
    }

    function processQueue() {
        var raw = sessionStorage.getItem(SESSION_KEY);
        if (!raw) return;
        var queue;
        try { queue = JSON.parse(raw); } catch(e) { sessionStorage.removeItem(SESSION_KEY); return; }
        if (!queue.items || !queue.items.length) {
            sessionStorage.removeItem(SESSION_KEY);
            setLog('All done!');
            setRunBtn(false);
            return;
        }

        waitFor(function() {
            return document.querySelectorAll('input[alt="Edit Row"]').length > 0 ? true : null;
        }).then(function() {
            var item = queue.items[0];
            var size = item.size, price = item.price;
            setLog('Updating ' + size + ' m3 -> $' + price);

            var priceRow = findPriceRowForSize(size);
            if (!priceRow) {
                setLog('Row not found: ' + size + ' - skipping');
                queue.items.shift();
                sessionStorage.setItem(SESSION_KEY, JSON.stringify(queue));
                setTimeout(processQueue, 300);
                return;
            }

            var editBtn = priceRow.querySelector('input[alt="Edit Row"]');
            editBtn.click();

            waitFor(function() {
                var inputs = priceRow.querySelectorAll('input[type="text"]');
                return inputs.length > 0 ? inputs : null;
            }, 6000).then(function(inputs) {
                var input = inputs[0];
                var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(input, String(price));
                input.dispatchEvent(new Event('input',  { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));

                var updateBtn = priceRow.querySelector('input[alt="Update Row"]');
                if (!updateBtn) {
                    var cancelBtn = priceRow.querySelector('input[alt="Cancel"]');
                    if (cancelBtn) cancelBtn.click();
                    setLog('No Update button for ' + size + ' - skipped');
                    queue.items.shift();
                    sessionStorage.setItem(SESSION_KEY, JSON.stringify(queue));
                    setTimeout(processQueue, 600);
                    return;
                }

                queue.items.shift();
                sessionStorage.setItem(SESSION_KEY, JSON.stringify(queue));
                updateBtn.click();
                setTimeout(processQueue, 2000);

            }).catch(function(e) {
                setLog(size + ': ' + e.message);
                queue.items.shift();
                sessionStorage.setItem(SESSION_KEY, JSON.stringify(queue));
                setTimeout(processQueue, 300);
            });
        }).catch(function(e) {
            setLog('Table not ready: ' + e.message);
            setRunBtn(false);
        });
    }

    function buildPanel() {
        if (document.getElementById('bb-panel')) return;

        var p = document.createElement('div');
        p.id = 'bb-panel';
        p.style.cssText = 'position:fixed;top:20px;right:20px;width:310px;background:#1e1e2e;color:#cdd6f4;border:1px solid #585b70;border-radius:10px;padding:14px 16px;font-family:sans-serif;font-size:13px;z-index:99999;box-shadow:0 6px 28px rgba(0,0,0,.6);line-height:1.5;';

        var hdr = document.createElement('div');
        hdr.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;';
        var ttl = document.createElement('b');
        ttl.style.color = '#cba6f7';
        ttl.textContent = 'Rate Updater';
        var minB = document.createElement('button');
        minB.id = 'bb-min';
        minB.textContent = '-';
        minB.style.cssText = 'background:none;border:none;color:#cdd6f4;cursor:pointer;font-size:20px;padding:0;line-height:1;';
        hdr.appendChild(ttl);
        hdr.appendChild(minB);
        p.appendChild(hdr);

        var body = document.createElement('div');
        body.id = 'bb-body';

        var lbl = document.createElement('div');
        lbl.style.cssText = 'color:#a6e3a1;margin-bottom:6px;font-size:12px;';
        lbl.textContent = 'Paste price table (from SkipBin app):';
        body.appendChild(lbl);

        var ta = document.createElement('textarea');
        ta.id = 'bb-input';
        ta.rows = 7;
        ta.placeholder = 'Bin Size\tSearch Price\tWill Set To\n2 m3\t$179\t$178\n...';
        ta.style.cssText = 'width:100%;box-sizing:border-box;background:#181825;color:#cdd6f4;border:1px solid #585b70;border-radius:5px;padding:6px;font-size:11px;resize:vertical;';
        body.appendChild(ta);

        var btn = document.createElement('button');
        btn.id = 'bb-run';
        btn.textContent = 'Update Prices';
        btn.style.cssText = 'margin-top:8px;width:100%;padding:9px;background:#cba6f7;color:#1e1e2e;border:none;border-radius:5px;cursor:pointer;font-weight:bold;font-size:13px;';
        body.appendChild(btn);

        var log = document.createElement('div');
        log.id = 'bb-log';
        log.style.cssText = 'margin-top:7px;min-height:18px;color:#f9e2af;font-size:12px;word-break:break-word;';
        body.appendChild(log);

        p.appendChild(body);
        document.body.appendChild(p);
        console.log('[RateUpdater] panel built');

        minB.addEventListener('click', function() {
            body.style.display = (body.style.display === 'none') ? '' : 'none';
            minB.textContent  = (body.style.display === 'none') ? '+' : '-';
        });

        btn.addEventListener('click', function() {
            var text = ta.value;
            if (!text.trim()) { setLog('Paste the price table first.'); return; }
            var map = parseTable(text);
            if (!map) { setLog('Parse failed - need Bin Size and Will Set To columns.'); return; }
            var items = Object.keys(map).map(function(sz) { return { size: sz, price: map[sz] }; });
            sessionStorage.setItem(SESSION_KEY, JSON.stringify({ items: items }));
            setRunBtn(true);
            setLog('Queued ' + items.length + ' rows...');
            processQueue();
        });
    }

    buildPanel();

    if (sessionStorage.getItem(SESSION_KEY)) {
        setRunBtn(true);
        setLog('Resuming...');
        setTimeout(processQueue, 1200);
    }

})();
