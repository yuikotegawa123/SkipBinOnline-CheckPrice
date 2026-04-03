// ==UserScript==
// @name         BookABin Rate Updater
// @namespace    bookabin-rate-updater
// @version      3.0
// @description  Table UI to set prices on BookABin rates management page
// @match        *://*.bookabin.com.au/*
// @run-at       document-end
// @grant        none
// ==/UserScript==

(function () {
    var BIN_SIZES = ['2','3','4','4.5','6','7.5','9','11','15','30'];
    var stopFlag = false;

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

    function getDataRows() {
        var snap = document.evaluate(
            '//table[@id="dltRates"]/tbody/tr[position() mod 2 = 0]',
            document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
        );
        var rows = [];
        for (var i = 0; i < snap.snapshotLength; i++) rows.push(snap.snapshotItem(i));
        return rows;
    }

    function waitForRows() {
        return waitFor(function() {
            var snap = document.evaluate(
                '//table[@id="dltRates"]/tbody/tr[position() mod 2 = 0]',
                document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
            );
            return snap.snapshotLength > 0 ? true : null;
        }, 12000);
    }

    function findRowForSize(sz) {
        var pat = new RegExp('(?<![\\d.])' + sz.toString().replace('.', '\\.') + '(?![\\d.])', 'i');
        var rows = getDataRows();
        for (var i = 0; i < rows.length; i++) {
            if (pat.test(rows[i].innerText.replace(/\s+/g, ' '))) return rows[i];
        }
        return null;
    }

    function log(msg, color) {
        var el = document.getElementById('bb-log');
        if (!el) return;
        el.innerHTML += '<div style="color:' + (color || '#f9e2af') + '">' + msg + '</div>';
        el.scrollTop = el.scrollHeight;
        console.log('[RateUpdater] ' + msg);
    }

    function setRunBtn(disabled) {
        var b = document.getElementById('bb-run');
        if (b) b.disabled = disabled;
    }

    function setStopBtn(disabled) {
        var b = document.getElementById('bb-stop');
        if (b) b.disabled = disabled;
    }

    async function runUpdates(items) {
        stopFlag = false;
        setRunBtn(true);
        setStopBtn(false);
        var done = 0;
        for (var i = 0; i < items.length; i++) {
            if (stopFlag) { log('Stopped. ' + done + '/' + items.length + ' saved.', '#fab387'); break; }
            var sz = items[i].size, price = items[i].price;

            var row = findRowForSize(sz);
            if (!row) { log(sz + ' m3: row not found — skipping.', '#f38ba8'); continue; }

            var editBtn = row.querySelector('input[alt="Edit Row"], input[title="Edit Row"]');
            if (!editBtn) { log(sz + ' m3: Edit button not found — skipping.', '#f38ba8'); continue; }

            log('Updating ' + sz + ' m3 → $' + price);
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
                log(sz + ' m3: edit mode timed out — skipping.', '#f38ba8');
                var cc = document.querySelector('input[alt="Cancel"], input[title="Cancel"]');
                if (cc) cc.click();
                continue;
            }

            var priceInput = document.evaluate(
                './/td[@class="ratecelledit"][4]/input',
                editTr, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
            ).singleNodeValue;

            if (!priceInput) {
                log(sz + ' m3: price input not found — cancelling.', '#f38ba8');
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
            if (!updateBtn) { log(sz + ' m3: Update Row button missing.', '#f38ba8'); continue; }

            updateBtn.scrollIntoView({ block: 'center' });
            updateBtn.click();

            try { await waitForRows(); } catch(e) { await wait(2500); }
            await wait(400);
            done++;
            log('  Saved', '#a6e3a1');
        }
        if (!stopFlag) log('Done — ' + done + '/' + items.length + ' updated.', '#a6e3a1');
        setRunBtn(false);
        setStopBtn(true);
    }

    function buildPanel() {
        if (document.getElementById('bb-panel')) return;

        var p = document.createElement('div');
        p.id = 'bb-panel';
        p.style.cssText = 'position:fixed;top:20px;right:20px;width:300px;background:#1e1e2e;color:#cdd6f4;border:1px solid #585b70;border-radius:10px;padding:14px 16px;font-family:sans-serif;font-size:13px;z-index:99999;box-shadow:0 6px 28px rgba(0,0,0,.65);';

        var hdr = document.createElement('div');
        hdr.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;';
        var ttl = document.createElement('b'); ttl.style.color='#cba6f7'; ttl.textContent='Rate Updater';
        var minB = document.createElement('button'); minB.id='bb-min'; minB.textContent='-'; minB.style.cssText='background:none;border:none;color:#cdd6f4;cursor:pointer;font-size:20px;padding:0;line-height:1;';
        hdr.appendChild(ttl); hdr.appendChild(minB); p.appendChild(hdr);

        var body = document.createElement('div'); body.id='bb-body';

        // Table
        var tbl = document.createElement('table');
        tbl.style.cssText = 'width:100%;border-collapse:collapse;margin-bottom:8px;';
        var thead = tbl.createTHead(), hrow = thead.insertRow();
        ['Bin Size','Will Set To ($)'].forEach(function(t) {
            var th = document.createElement('th'); th.textContent=t;
            th.style.cssText='text-align:left;padding:4px 6px;font-size:11px;color:#a6adc8;border-bottom:1px solid #45475a;';
            hrow.appendChild(th);
        });
        var tbody = tbl.createTBody();
        BIN_SIZES.forEach(function(sz) {
            var tr = tbody.insertRow();
            var td1 = tr.insertCell(); td1.textContent = sz+' m3'; td1.style.cssText='padding:4px 6px;font-size:12px;border-bottom:1px solid #313244;';
            var td2 = tr.insertCell(); td2.style.cssText='padding:3px 6px;border-bottom:1px solid #313244;';
            var inp = document.createElement('input'); inp.type='number'; inp.id='bb-p-'+sz; inp.min='0'; inp.placeholder='skip';
            inp.style.cssText='width:100%;box-sizing:border-box;background:#181825;color:#cdd6f4;border:1px solid #585b70;border-radius:4px;padding:4px 6px;font-size:12px;';
            td2.appendChild(inp);
        });
        body.appendChild(tbl);

        // Buttons
        var btnRow = document.createElement('div'); btnRow.style.cssText='display:flex;gap:6px;margin-bottom:6px;';
        var clr  = document.createElement('button'); clr.textContent='Clear'; clr.style.cssText='flex:1;padding:7px;background:#313244;color:#cdd6f4;border:none;border-radius:5px;cursor:pointer;font-size:12px;';
        var run  = document.createElement('button'); run.id='bb-run'; run.textContent='Update Prices'; run.style.cssText='flex:2;padding:7px;background:#cba6f7;color:#1e1e2e;border:none;border-radius:5px;cursor:pointer;font-weight:bold;font-size:13px;';
        var stop = document.createElement('button'); stop.id='bb-stop'; stop.textContent='Stop'; stop.disabled=true; stop.style.cssText='flex:1;padding:7px;background:#45475a;color:#cdd6f4;border:none;border-radius:5px;cursor:pointer;font-size:12px;';
        btnRow.appendChild(clr); btnRow.appendChild(run); btnRow.appendChild(stop); body.appendChild(btnRow);

        var logDiv = document.createElement('div'); logDiv.id='bb-log'; logDiv.style.cssText='max-height:160px;overflow-y:auto;background:#181825;border:1px solid #313244;border-radius:5px;padding:6px;font-size:11px;line-height:1.7;display:none;';
        body.appendChild(logDiv);

        p.appendChild(body);
        document.body.appendChild(p);
        console.log('[RateUpdater] panel built');

        minB.addEventListener('click', function() {
            body.style.display = body.style.display==='none' ? '' : 'none';
            minB.textContent   = body.style.display==='none' ? '+' : '-';
        });
        clr.addEventListener('click', function() {
            BIN_SIZES.forEach(function(sz){ var e=document.getElementById('bb-p-'+sz); if(e)e.value=''; });
            logDiv.innerHTML=''; logDiv.style.display='none';
        });
        stop.addEventListener('click', function() { stopFlag = true; });
        run.addEventListener('click', function() {
            var items = [];
            BIN_SIZES.forEach(function(sz) {
                var e = document.getElementById('bb-p-'+sz);
                if (!e || e.value.trim()==='') return;
                var pr = parseInt(e.value, 10);
                if (!isNaN(pr)) items.push({size: sz, price: pr});
            });
            if (!items.length) { logDiv.innerHTML='<div style="color:#f38ba8">Enter at least one price.</div>'; logDiv.style.display=''; return; }
            logDiv.innerHTML=''; logDiv.style.display='';
            runUpdates(items);
        });
    }

    function init() {
        if (window.location.href.toLowerCase().indexOf('rates_manage') === -1) return;
        console.log('[RateUpdater] init');
        buildPanel();
    }

    if (document.readyState === 'loading') {
        window.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();