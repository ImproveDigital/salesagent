/**
 * Publishers page — Sprint 7 Phase 2.
 *
 * Extracted from tenant_settings.js when the Publishers section
 * promoted out of Tenant Settings into a Configure → Workspace peer
 * page. Same AJAX endpoints under /tenant/<id>/publisher-partners — the
 * publisher_partners blueprint owns the API; this script owns the UI.
 *
 * Reads config from #settings-config data attributes (same convention
 * the other settings-derived pages use).
 */

const config = (function () {
    const el = document.getElementById('settings-config');
    if (!el) {
        console.error('Publishers config element not found');
        return {};
    }
    return {
        scriptName: el.dataset.scriptName || '',
        tenantId: el.dataset.tenantId || '',
        isEmbedded: el.dataset.isEmbedded === 'true',
    };
})();

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function copyAgentUrlToClipboard(button) {
    const el = document.getElementById('public-agent-url-display');
    if (!el) return;
    const showCopied = () => {
        if (!button) return;
        const defaultLabel = button.dataset.defaultLabel || button.textContent;
        button.dataset.defaultLabel = defaultLabel;
        clearTimeout(button._copyResetTimer);
        button.textContent = 'Copied!';
        button._copyResetTimer = setTimeout(() => {
            button.textContent = button.dataset.defaultLabel;
        }, 2000);
    };

    navigator.clipboard.writeText(el.textContent.trim()).then(showCopied).catch(() => {
        // Fallback: select the text so the user can manually copy.
        const range = document.createRange();
        range.selectNode(el);
        window.getSelection().removeAllRanges();
        window.getSelection().addRange(range);
    });
}

// AAO status chip styles, keyed by aao_status. See salesagent#377 for
// the operational rationale on each state. "stale" is a transitional
// UI-only state for rows that pre-date the AAO counts columns.
const AAO_STATUS_STYLES = {
    authorized: { bg: '#d1fae5', fg: '#065f46', label: 'Authorized' },
    // Non-conformant file but products bind — publisher's entry lacks
    // authorization_type and we resolve permissively to top-level
    // properties[]. Yellow rather than red because the row works.
    unbound: { bg: '#fef3c7', fg: '#92400e', label: 'Authorized (non-conformant file)' },
    pending: { bg: '#fef3c7', fg: '#92400e', label: 'Pending' },
    // File fetched but exposes zero properties — operator can't do
    // anything until the publisher adds a properties[] block.
    no_properties: { bg: '#fee2e2', fg: '#991b1b', label: 'No properties listed' },
    unreachable: { bg: '#fee2e2', fg: '#991b1b', label: 'Unreachable' },
    stale: { bg: '#e0e7ff', fg: '#3730a3', label: 'Refresh needed' },
    // Authorized by an admin without an adagents.json listing. Kept across
    // refreshes until the publisher authorizes us for real.
    manual: { bg: '#e0f2fe', fg: '#075985', label: 'Manually authorized' },
};

const MANUAL_AUTHORIZATION_WARNING =
    'Not listed in the publisher’s adagents.json. Buyer agents that check it may skip this publisher.';

function aaoStatusChip(kind) {
    const s = AAO_STATUS_STYLES[kind] || AAO_STATUS_STYLES.stale;
    return `<span style="display: inline-block; padding: 0.25rem 0.5rem; background: ${s.bg}; color: ${s.fg}; border-radius: 4px; font-size: 0.75rem; font-weight: 600;">${s.label}</span>`;
}

function relativeTime(iso) {
    if (!iso) return 'Never';
    const then = new Date(iso).getTime();
    const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}

function loadPublishers() {
    const container = document.getElementById('publishers-list');
    const summary = document.getElementById('publishers-summary');
    if (!container) return;

    container.innerHTML = `
        <div style="text-align: center; padding: 2rem; color: #6b7280;">
            <span style="font-size: 1.5rem;">⏳</span>
            <p>Loading publishers...</p>
        </div>
    `;

    fetch(`${config.scriptName}/tenant/${config.tenantId}/publisher-partners`, {
        credentials: 'same-origin',
    })
        .then((response) => response.json())
        .then((data) => {
            if (data.error) {
                container.innerHTML = `
                    <div style="text-align: center; padding: 2rem; color: #dc2626;">
                        <span style="font-size: 1.5rem;">❌</span>
                        <p>Error: ${escapeHtml(data.error)}</p>
                    </div>
                `;
                if (summary) summary.innerHTML = '';
                return;
            }

            if (summary) {
                if (!data.partners || data.partners.length === 0) {
                    summary.innerHTML = '';
                } else {
                    summary.innerHTML = `
                        <div style="padding: 0.75rem 1rem; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; color: #065f46;">
                            <strong>${data.partners.length} publisher partner${data.partners.length === 1 ? '' : 's'}</strong>
                            · <strong>${data.authorized_properties || 0}</strong> propert${data.authorized_properties === 1 ? 'y' : 'ies'} authorized to your agent
                            (of <strong>${data.total_properties || 0}</strong> listed)
                        </div>
                    `;
                }
            }

            if (!data.partners || data.partners.length === 0) {
                container.innerHTML = `
                    <div style="text-align: center; padding: 3rem; background: #f9fafb; border: 2px dashed #e5e7eb; border-radius: 8px;">
                        <span style="font-size: 2rem;">🌐</span>
                        <h3 style="margin: 1rem 0 0.5rem 0; color: #374151;">No Publishers Yet</h3>
                        <p style="color: #6b7280; margin-bottom: 1rem;">Add your first publisher partner to start selling their inventory.</p>
                        <button onclick="showAddPublisherModal()" class="btn btn-primary">+ Add Publisher</button>
                    </div>
                `;
                return;
            }

            const isEmbedded = config.isEmbedded;

            let html = `
                <table style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="border-bottom: 2px solid #e5e7eb;">
                            <th style="text-align: left; padding: 0.75rem; font-weight: 600; color: #374151;">Publisher</th>
                            <th style="text-align: left; padding: 0.75rem; font-weight: 600; color: #374151;">Status</th>
                            <th style="text-align: left; padding: 0.75rem; font-weight: 600; color: #374151;">Authorized / listed</th>
                            <th style="text-align: left; padding: 0.75rem; font-weight: 600; color: #374151;">Last refreshed</th>
                            <th style="text-align: right; padding: 0.75rem; font-weight: 600; color: #374151;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
            `;

            data.partners.forEach((partner) => {
                // Server is source of truth for aao_status — never been
                // refreshed → 'stale'. The legacy is_verified fallback
                // misled new partners ('pending' suggests publisher
                // rejected us when really we just haven't asked yet).
                const statusKind = partner.aao_status || 'stale';
                const chip = aaoStatusChip(statusKind);
                const errorMsg = partner.last_fetch_error || partner.sync_error;
                const counts =
                    statusKind === 'stale' && !partner.last_refreshed_at
                        ? '<span style="color: #9ca3af;">— / —</span>'
                        : `<strong>${partner.authorized_properties || 0}</strong> / ${partner.total_properties || 0}`;
                const refreshed = relativeTime(partner.last_refreshed_at || partner.last_synced_at);
                const manualHint = partner.manual_authorization
                    ? `<div style="font-size: 0.75rem; color: #075985; margin-top: 0.25rem;">${MANUAL_AUTHORIZATION_WARNING}</div>`
                    : '';
                const onboardingHint =
                    statusKind === 'pending' ||
                    statusKind === 'unreachable' ||
                    statusKind === 'no_properties' ||
                    statusKind === 'unbound' ||
                    statusKind === 'manual'
                        ? `<div style="margin-top: 0.5rem; font-size: 0.75rem;">
                               <a href="${escapeHtml(partner.aao_onboarding_url)}" target="_blank" rel="noopener" style="color: #2563eb;">
                                   Send AAO link to publisher →
                               </a>
                           </div>`
                        : '';

                let manualButton = '';
                if (partner.manual_authorization) {
                    manualButton = `
                        <button onclick="removeManualAuthorization(${partner.id}, '${escapeHtml(partner.publisher_domain)}')"
                                class="btn btn-sm" style="background: #f3f4f6; color: #374151; border: none; padding: 0.25rem 0.5rem; border-radius: 4px; cursor: pointer; margin-right: 0.25rem;">
                            Remove manual authorization
                        </button>`;
                } else if (statusKind !== 'authorized' && statusKind !== 'unbound') {
                    // Keyed on the visible status, not is_verified: dev/mock
                    // auto-verified rows still show "Pending" and need the button.
                    manualButton = `
                        <button onclick="authorizePublisherManually(${partner.id}, '${escapeHtml(partner.publisher_domain)}')"
                                class="btn btn-sm" style="background: #e0f2fe; color: #075985; border: none; padding: 0.25rem 0.5rem; border-radius: 4px; cursor: pointer; margin-right: 0.25rem;">
                            Authorize manually
                        </button>`;
                }

                const actionButtons = isEmbedded
                    ? '<span style="color: #9ca3af; font-size: 0.75rem;">Platform-managed</span>'
                    : `
                        ${manualButton}
                        <button onclick="refreshPublisher(${partner.id})"
                                class="btn btn-sm" style="background: #eff6ff; color: #1e40af; border: none; padding: 0.25rem 0.5rem; border-radius: 4px; cursor: pointer; margin-right: 0.25rem;">
                            Refresh
                        </button>
                        <button onclick="deletePublisher(${partner.id}, '${escapeHtml(partner.publisher_domain)}')"
                                class="btn btn-sm" style="background: #fee2e2; color: #991b1b; border: none; padding: 0.25rem 0.5rem; border-radius: 4px; cursor: pointer;">
                            Delete
                        </button>
                    `;

                html += `
                    <tr style="border-bottom: 1px solid #f3f4f6;" data-partner-id="${partner.id}">
                        <td style="padding: 0.75rem;">
                            <div style="font-weight: 600;">${escapeHtml(partner.display_name || partner.publisher_domain)}</div>
                            <div style="font-size: 0.875rem; color: #6b7280;">${escapeHtml(partner.publisher_domain)}</div>
                        </td>
                        <td style="padding: 0.75rem;">
                            ${chip}
                            ${errorMsg ? `<div style="font-size: 0.75rem; color: #dc2626; margin-top: 0.25rem;">${escapeHtml(errorMsg)}</div>` : ''}
                            ${manualHint}
                            ${onboardingHint}
                        </td>
                        <td style="padding: 0.75rem; color: #374151;">${counts}</td>
                        <td style="padding: 0.75rem; color: #6b7280;">${refreshed}</td>
                        <td style="padding: 0.75rem; text-align: right; white-space: nowrap;">
                            ${actionButtons}
                        </td>
                    </tr>
                `;
            });

            html += '</tbody></table>';
            container.innerHTML = html;
        })
        .catch((error) => {
            container.innerHTML = `
                <div style="text-align: center; padding: 2rem; color: #dc2626;">
                    <span style="font-size: 1.5rem;">❌</span>
                    <p>Error loading publishers: ${escapeHtml(error.message)}</p>
                </div>
            `;
        });
}

function showAddPublisherModal() {
    const modal = document.getElementById('add-publisher-modal');
    if (modal) {
        modal.style.display = 'flex';
        document.getElementById('publisher-domain').focus();
    }
}

function hideAddPublisherModal() {
    const modal = document.getElementById('add-publisher-modal');
    if (modal) {
        modal.style.display = 'none';
        document.getElementById('add-publisher-form').reset();
    }
}

document.addEventListener('click', function (e) {
    const modal = document.getElementById('add-publisher-modal');
    if (modal && e.target === modal) {
        hideAddPublisherModal();
    }
});

function addPublisher(event) {
    event.preventDefault();

    const domain = document.getElementById('publisher-domain').value.trim();
    const displayName = document.getElementById('publisher-display-name').value.trim();
    const manualCheckbox = document.getElementById('publisher-manual-authorization');
    const authorizeManually = manualCheckbox ? manualCheckbox.checked : false;
    const submitBtn = document.getElementById('add-publisher-submit');

    if (!domain) {
        alert('Please enter a publisher domain');
        return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = 'Adding...';

    fetch(`${config.scriptName}/tenant/${config.tenantId}/publisher-partners`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            publisher_domain: domain,
            display_name: displayName || domain,
        }),
    })
        .then((response) => response.json())
        .then((data) => {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Add Publisher';

            if (data.error) {
                alert('Error: ' + data.error);
                return;
            }

            hideAddPublisherModal();

            // The add endpoint never authorizes manually; that is a separate,
            // admin-only call so the role check and audit log live in one place.
            if (authorizeManually && !data.is_verified) {
                return postManualAuthorization(data.id, 'POST').then(() => loadPublishers());
            }

            loadPublishers();

            if (data.message) {
                alert(data.message);
            }
        })
        .catch((error) => {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Add Publisher';
            alert('Error: ' + error.message);
        });
}

function deletePublisher(partnerId, domain) {
    if (!confirm(`Are you sure you want to remove ${domain}? This will remove authorization to sell their inventory.`)) {
        return;
    }

    fetch(`${config.scriptName}/tenant/${config.tenantId}/publisher-partners/${partnerId}`, {
        method: 'DELETE',
        credentials: 'same-origin',
    })
        .then((response) => response.json())
        .then((data) => {
            if (data.error) {
                alert('Error: ' + data.error);
                return;
            }
            loadPublishers();
        })
        .catch((error) => {
            alert('Error: ' + error.message);
        });
}

function syncAllPublishers() {
    const btn = document.getElementById('sync-publishers-btn');
    const icon = document.getElementById('sync-publishers-icon');

    btn.disabled = true;
    icon.style.animation = 'spin 1s linear infinite';

    fetch(`${config.scriptName}/tenant/${config.tenantId}/publisher-partners/sync`, {
        method: 'POST',
        credentials: 'same-origin',
    })
        .then((response) => response.json())
        .then((data) => {
            btn.disabled = false;
            icon.style.animation = '';

            if (data.error) {
                alert('Error: ' + data.error);
                return;
            }

            loadPublishers();

            if (data.results) {
                const verified = data.results.filter((r) => r.is_verified).length;
                const failed = data.results.filter((r) => !r.is_verified).length;
                alert(`Verification complete: ${verified} verified, ${failed} not verified`);
            }
        })
        .catch((error) => {
            btn.disabled = false;
            icon.style.animation = '';
            alert('Error: ' + error.message);
        });
}

// Manual authorization: POST authorizes, DELETE removes it. Resolves to the
// updated partner, or null after showing the error.
function postManualAuthorization(partnerId, method) {
    return fetch(`${config.scriptName}/tenant/${config.tenantId}/publisher-partners/${partnerId}/manual-authorization`, {
        method: method,
        credentials: 'same-origin',
    })
        .then((r) => r.json())
        .then((data) => {
            if (data.error) {
                alert('Error: ' + data.error);
                return null;
            }
            return data;
        })
        .catch((err) => {
            alert('Error: ' + err.message);
            return null;
        });
}

function authorizePublisherManually(partnerId, domain) {
    const ok = confirm(
        `Authorize ${domain} manually?\n\n` +
            'Use this when the publisher has agreed to work with you but has not yet added your agent URL ' +
            'to their adagents.json. Their inventory can then be used in products.\n\n' +
            'Buyer agents that check adagents.json may still skip this publisher until it lists your agent URL.'
    );
    if (!ok) return;
    postManualAuthorization(partnerId, 'POST').then((data) => {
        if (data) loadPublishers();
    });
}

function removeManualAuthorization(partnerId, domain) {
    if (
        !confirm(
            `Remove the manual authorization for ${domain}?\n\n` +
                'Its properties are removed and can no longer be added to products. ' +
                'It goes back to Pending until its adagents.json lists your agent URL.'
        )
    ) {
        return;
    }
    postManualAuthorization(partnerId, 'DELETE').then((data) => {
        if (data) loadPublishers();
    });
}

// Refresh a single publisher (forces fresh AAO fetch).
function refreshPublisher(partnerId) {
    const row = document.querySelector(`tr[data-partner-id="${partnerId}"]`);
    if (row) row.style.opacity = '0.5';

    fetch(`${config.scriptName}/tenant/${config.tenantId}/publisher-partners/${partnerId}/refresh`, {
        method: 'POST',
        credentials: 'same-origin',
    })
        .then((r) => r.json())
        .then((data) => {
            if (data.error) {
                alert('Refresh failed: ' + data.error);
                if (row) row.style.opacity = '1';
                return;
            }
            loadPublishers();
        })
        .catch((err) => {
            alert('Refresh failed: ' + err.message);
            if (row) row.style.opacity = '1';
        });
}

document.addEventListener('DOMContentLoaded', loadPublishers);
