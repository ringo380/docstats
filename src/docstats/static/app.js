    document.addEventListener('htmx:responseError', function(e) {
        var target = e.detail.target;
        if (!target) return;
        var status = e.detail.xhr ? e.detail.xhr.status : 0;
        var msg;
        if (status === 0) {
            msg = 'Network error. Check your connection and try again.';
        } else if (status === 408 || status === 504) {
            msg = 'The request timed out. The NPI Registry may be slow \u2014 try again.';
        } else {
            msg = 'Something went wrong (error ' + status + '). Please try again.';
        }
        var div = document.createElement('div');
        div.className = 'flash flash-error';
        div.textContent = msg;
        target.replaceChildren(div);
    });
    (function() {
        var trigger = document.getElementById('profile-trigger');
        var menu = document.getElementById('profile-menu');
        if (trigger && menu) {
            trigger.addEventListener('click', function(e) {
                e.stopPropagation();
                var open = menu.classList.toggle('open');
                trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
            });
            document.addEventListener('click', function(e) {
                if (!menu.contains(e.target)) {
                    menu.classList.remove('open');
                    trigger.setAttribute('aria-expanded', 'false');
                }
            });
        }
    })();
    function toggleNoteEdit(npi) {
        var display = document.getElementById('notes-display-' + npi);
        var edit = document.getElementById('notes-edit-' + npi);
        if (!display || !edit) return;
        var showing = edit.style.display !== 'none';
        display.style.display = showing ? '' : 'none';
        edit.style.display = showing ? 'none' : '';
        if (!showing) edit.querySelector('textarea').focus();
    }
