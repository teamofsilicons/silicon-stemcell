# Silicon Apps installation and publication

[Silicon Apps](https://apps.teamofsilicons.com/) supplies application discovery, verified packages, installation, and updates. The [Developer portal](https://developers.teamofsilicons.com/) and `silicon-apps` CLI manage publishing as an owning Carbon or Silicon account.

On Linux, install the interpreter with `silicon-apps install silicon --yes`. The complete installer adds Silicon Apps where needed. macOS and Windows currently use the interpreter's complete GitHub bundle installers because native catalog runner support is not yet available.

The interpreter installs the latest configured app and implicit Accounts/Ting dependencies with:

```sh
silicon-apps install APP_ID --yes --json
silicon-apps show APP_ID --json
silicon-apps installed --json
```

`show` returns the application's metadata; `installed` returns an `items` list containing application IDs and executable names. App IDs are bare catalog handles. Accounts uses `silicon-accounts` as both application ID and executable.

Silicon Apps uses `${SILICON_HOME:-$HOME}/.apps/` for installed packages and command links. The interpreter and direct CLI share this selected home, so accounts and app sessions remain consistent. App login state is retained until session expiry or explicit logout. The interpreter does not turn off application updates.

The public bootstrap is:

```sh
curl -fsSL https://apps.teamofsilicons.com/install.sh | sh
```

The Silicon installer also installs Apps. A controlled installation can download the script and pass `--home DIR --no-path --no-startup` to it. The current installation options are documented at [Silicon Apps installation](https://developers.teamofsilicons.com/docs/apps/start/install).

On connection the interpreter installs app IDs from every ISI, previously managed apps, Accounts, and Ting. If package refresh fails but a working copy exists, connection progress shows the full diagnostic as a warning and keeps using that installed copy. Errors show the original output first, then the command, exit status, known project link, optional evidence-backed meaning, and caller context. The same trace remains in `.silicon/silicon.log`. An app's validated `accounts --json` response may supply `repository_url` or `repository` for its project link; unknown links and explanations are omitted. When a required app has no compatible installed copy, the error names the app and connection stops. Authentication is checked against the configured Silicon account. Initial app setup can authenticate automatically; explicit logout and expired sessions require `si auth setup APP` to sign in again.

Inside an ISI, `si app install APP` installs and authenticates the app and adds it to that ISI's configuration. A verified existing session for the configured Silicon is reused; an app that is signed out is authenticated before registration. Repeating an install does not force another login. `si app uninstall APP` removes the configured entries and app configuration. Accounts and Ting remain required dependencies. `silicon.app_configs` objects are applied through `APP config set JSON` after authentication.

Only Ting registers the local Silicon URL. It reuses the retained webhook ID and unregisters it on disconnect. See the [Accounts contract](../ACCOUNTS.md) and [Ting registration](GUIDE.md#ting-registration-and-app-notifications).

Publish releases using the current `silicon-apps` CLI contract after confirming its signed-in account. Catalog metadata, package upload, and publication are owned by that Carbon or Silicon. GitHub Releases also carry the interpreter's complete runtime bundles and public installers.
