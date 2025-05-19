document.addEventListener('DOMContentLoaded', () => {
    const maxVersion = window.datasette_chronicle_max_version;
    const databaseName = window.datasette_chronicle_database_name;
    const tableName = window.datasette_chronicle_table_name;

    if (typeof maxVersion === 'undefined' || typeof databaseName === 'undefined' || typeof tableName === 'undefined') {
        // If any of these are undefined, the necessary data wasn't injected.
        // This might happen on pages where extra_body_script isn't called or doesn't add these.
        console.log('Datasette Chronicle: Missing required global variables. Exiting.');
        return;
    }

    const localStorageKey = `chronicle_last_seen_info_${databaseName}_${tableName}`;
    let storedInfoText = localStorage.getItem(localStorageKey);
    let storedInfo = {};

    if (storedInfoText) {
        try {
            storedInfo = JSON.parse(storedInfoText);
        } catch (e) {
            console.error('Datasette Chronicle: Error parsing storedInfo from localStorage', e);
            storedInfo = {}; // Reset to default if parsing fails
        }
    }

    const lastSeenVersion = storedInfo.version;
    const lastSeenTimestamp = storedInfo.timestamp;

    function displayBanner(count, timeSinceText) {
        const banner = document.createElement('div');
        banner.className = 'chronicle-notification-banner'; // Use the class defined in extra_head_html
        let message = `${count} row${count > 1 ? 's' : ''} updated`;
        if (timeSinceText) {
            message += ` since your last visit ${timeSinceText}.`;
        } else {
            message += '.'; // Should ideally not happen if timeSinceText is always calculated
        }
        
        banner.textContent = message;

        // Try to prepend to a common Datasette content container, fallback to body
        const mainContent = document.querySelector('.table-wrapper') || document.querySelector('div[role="main"]') || document.querySelector('#main-content');
        if (mainContent && mainContent.firstChild) {
            mainContent.insertBefore(banner, mainContent.firstChild);
        } else if (document.body.firstChild) {
            document.body.insertBefore(banner, document.body.firstChild);
        } else {
            document.body.appendChild(banner);
        }
    }

    if (typeof lastSeenVersion === 'undefined' || maxVersion > lastSeenVersion) {
        if (typeof lastSeenVersion !== 'undefined') {
            // This means it's not the first visit AND there are new changes.
            const apiUrl = `./${encodeURIComponent(tableName)}.json?_since=${encodeURIComponent(lastSeenVersion)}&_extra=count&_size=0`;
            
            fetch(apiUrl)
                .then(response => {
                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }
                    return response.json();
                })
                .then(data => {
                    if (data && data.ok && data.count > 0) {
                        const count = data.count;
                        let timeSince = "";
                        if (lastSeenTimestamp) {
                            const diffMs = new Date().getTime() - lastSeenTimestamp;
                            const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
                            const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

                            if (diffDays > 0) {
                                timeSince = `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;
                            } else if (diffHours > 0) {
                                timeSince = `${diffHours} hour${diffHours > 1 ? 's' : ''} ago`;
                            } else {
                                timeSince = "recently";
                            }
                        } else {
                            // If no lastSeenTimestamp, perhaps just say "new changes"
                            timeSince = "recently (new changes detected)";
                        }
                        displayBanner(count, timeSince);
                    }
                })
                .catch(error => {
                    console.error('Datasette Chronicle: Error fetching chronicle count:', error);
                });
        }
        // Always update localStorage if it's the first visit or if there are new versions
        localStorage.setItem(localStorageKey, JSON.stringify({
            version: maxVersion,
            timestamp: new Date().getTime()
        }));

    } else {
        // This case is when maxVersion <= lastSeenVersion.
        // If it's the very first visit and maxVersion is 0 (empty chronicle table),
        // we still want to record this visit so subsequent additions show up as new.
        // The condition `typeof lastSeenVersion === 'undefined'` is handled by the `if` block above.
        // So, this `else` block implies `lastSeenVersion` is defined and `maxVersion <= lastSeenVersion`.
        // No action needed here other than what might already be done (e.g. ensuring timestamp updated if desired, but current logic updates timestamp only on new versions or first visit)
    }
});
