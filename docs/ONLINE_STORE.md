# WMP Online Store integration — research and implementation notes

This document records **what is known, how it is known, and what is still a
guess** about Windows Media Player's legacy Online Store subsystem, plus the
reasoning behind the architecture in `online_store/`.

Every claim below is tagged:

| Tag | Meaning |
|---|---|
| **[DOC]** | Stated in Microsoft's archived WMP SDK documentation. Cited by page title. |
| **[VERIFIED]** | Confirmed by inspecting binaries on *this* machine (Windows 11, WMP 12.0.26100). Method given. |
| **[INFERRED]** | A conclusion drawn from [DOC] + [VERIFIED]. Not proven. The thing to check first if the store does not appear. |
| **[TODO]** | Not done, and why. |

---

## 1. Why Type 2 commerce, and why there is no COM DLL

**[DOC]** WMP supports two online-store architectures:

| | Type 2 commerce | Type 2 music | Type 1 |
|---|---|---|---|
| Service task panes | yes | yes | yes |
| `IWMPSubscriptionService` plug-in | **No** | yes | No |
| `IWMPContentPartner` | No | No | yes |
| Downloaded catalog | No | No | yes |
| Player version | 9+ | 10+ | 11+ |

*(Source: "Windows Media Player Online Stores" — the feature-comparison table.)*

**[DOC]** The plug-in exists to vend DRM licences: "Windows Media Player
inspects the ContentDistributor attribute... checks the registry to see whether
that online store has provided a type 2 plug-in... calls its methods to
determine whether the user has the rights to play the song." *(Source: "Type 2
Online Store Plug-in".)*

**Consequence:** a commerce store — one that just shows a webpage and sells
things through it — needs **no COM component at all**. The registry reference
lists `SubscriptionObjectGUID` and an `InprocServer32` CLSID because it is
written for music stores; for a commerce store those describe a plug-in that
does not exist.

So `online_store/registry.py` writes `SubscriptionObjectGUID` because the
documented layout calls for it, sets `Capabilities = 0` because advertising
callbacks we do not implement would have WMP call into nothing, and **does not
register a CLSID by default**. A CLSID pointing at a non-existent DLL would be
a lie the player might act on. `--register-plugin-dll` exists for a future
music store that genuinely ships one.

**[INFERRED]** Nothing in the Type 2 commerce path *requires* COM. If WMP turns
out to demand the CLSID anyway, that shows up immediately as the store
appearing but failing to open, and the fix is local to `registry.install()`.

---

## 2. How a store is discovered

**[DOC]** WMP 10 and 11 read a **ServiceInfo XML document** to configure the
store's pages, button text, colours and images. In WMP 11 there is no set of
service task panes — there is a single **Online Stores tab**, which is
`ServiceTask1`; `ServiceTask2`/`ServiceTask3` are ignored. *(Sources:
"Service Task Panes", "ServiceInfo Document".)*

**[DOC]** In development, "your online store will appear in Windows Media Player
only if your test key or your production key is in the registry on the user's
computer", placed at:

```
[HKEY_CURRENT_USER\Software\Microsoft\MediaPlayer\Services]
"TestParameter" = "key1;key2;...;keyN"
```

*(Source: "Test and Production Keys for a Type 2 Online Store", "Registry Keys
and Entries for a Type 2 Online Store".)*

**[DOC]** Store identity lives at:

```
[HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\MediaPlayer\Subscriptions\keyName]
"Capabilities"           = dword:flags
"SubscriptionObjectGUID" = clsid
"FriendlyName"           = friendlyName
```

*(Source: "Registry Keys and Entries for a Type 2 Online Store".)*

**[DOC]** The `Key` attribute of `<ServiceInfo>` must match the registry
`keyName`: "This value must match the key name in the Key attribute of the
ServiceInfo element of the ServiceInfo document." *(Source: "Setup
Command-line Parameters for Online Stores".)*

**[VERIFIED]** WMP 12 on Windows 11 still contains this machinery. Reading the
UTF-16 string tables of the installed binaries:

* `C:\Windows\winsxs\amd64_microsoft-windows-mediaplayer-core_*\wmp.dll`
  contains `ServiceInfo`, `ServiceInfoRefresh`, `ServiceInfoTimeout`,
  `OnlineStore`, `SubscriptionObjectGuid`, `ActiveService`, `ActiveServiceName`,
  `TestParameter`, `DefaultSubscriptionService`, `ContentPartner`,
  `\ServiceTask1`, `eAllServicesUrl_Win7`, `AllServicesCheck`,
  `AllServicesRefresh`, `AllServicesTimeout`, and the debug string
  `CWMPBrandManager::DownloadAllServices() - URL:%s`.
* `wmplayer.exe` and `wmpshell.dll.mui` contain **zero** occurrences of
  `Online`, `Store`, `ServiceInfo` or `Subscription`.

**[INFERRED]** The online-store code lives in the `wmp.dll` core library rather
than in the shell, so the absence of UI strings in `wmplayer.exe` is expected
and is *not* evidence that the feature is gone. It does mean the Online Stores
tab is drawn by core code whose behaviour cannot be read off a string table.

---

## 3. Pointing WMP at a locally-served ServiceInfo document

This is the part that is **not** documented, and the most important thing to
confirm empirically.

Microsoft's model was that a store developer **submits a ServiceInfo URL to
Microsoft**, who host it and hand WMP a list. That service is gone. To run a
store from a local Flask process, something else has to tell WMP where to
fetch.

**[VERIFIED]** `C:\Program Files\Windows Media Player\setup_wm.exe` — the
component that installs a store from a **local** ServiceInfo document via
`/DefaultService` — contains, among others:

```
%sserviceinfo.xml          %sallservices.xml
Software\Microsoft\MediaPlayer\Services\%s
BASEURL
/DefaultService   /ServiceInfo   /ServiceExtra   /NoService   /CreateService
```

and, in one contiguous run of its string table, the `Services\%s` value names
`FriendlyName`, `ColorPlayer`, `ColorPlayerText`, `ImageLargeURL`,
`ImageMenuURL`, `ImageSmallURL`, `ContentPartner`, `Task1ButtonText`,
`Task3ButtonText`, `Task1ButtonTip`, `Task3ButtonTip`, `Type`.

**[VERIFIED]** `wmp.dll` contains `baseURL`, `ServiceInfoRefresh` and
`ServiceInfoTimeout`.

**[INFERRED]** WMP resolves `<BASEURL>serviceinfo.xml`, and `Services\<keyName>`
is the per-store key that supplies `BASEURL`. Two adjacent literals in
Microsoft's own installer plus a `baseURL` field in the player core is strong
evidence, but it is a pattern match, not a specification.

**How this project makes the guess cheap to test:**

* `BASEURL` is written explicitly by the installer, so it is one registry value
  to change if the guess is wrong.
* Both `/online-store/serviceinfo.xml` **and** `/online-store/serviceinfo` are
  served, so either half of the concatenation can be wrong independently.
* `/online-store/api/status` reports the effective `BASEURL`, the ServiceInfo
  URL, and whether WMP has actually been seen asking for the document.
* **[TODO]** The decisive test is empirical: install the store, start the
  server, open WMP's Online Stores tab, and check whether
  `/online-store/serviceinfo.xml` appears in `fai_server.log` under the `[REQ]`
  tag. A hit proves WMP found the document. Silence means `BASEURL` or the
  `TestParameter` gate is wrong, and the next thing to try is the documented
  `/DefaultService` setup parameter, which `setup_wm.exe` still implements.

---

## 4. Choices made, and why

**Type 2 commerce, not Type 1.** Type 1 needs `IWMPContentPartner`, a
downloaded catalog and deeper UI integration. Commerce needs one page. The
brief asked for the simplest architecture that is genuinely useful.

**A Blueprint, not a second server.** `FAI Server.py` already owns port 80,
port 443, the hosts-file redirection, the TLS certificate and the request log.
A second process would be a second thing to start and a second thing to forget.
The store is mounted with `app.register_blueprint()` and reached at
`/online-store/…` on the same listener.

**Disabled by default.** A checkout with no `online_store.ini` behaves exactly
as it did before this subsystem existed. This is asserted by the test suite
(section 7 of `test_online_store.py`), because a feature that is switched off
must not be able to take the FAI feature down with it.

**Synthesised audio, not shipped audio.** Every track is a decaying sine tone
generated with the stdlib `wave` module from a hash of its own id. The
repository stays small, the content is provably not anyone's copyrighted
recording, and the delivery path (provider → disk → playable file) is real
rather than mocked. A test opens the result with `wave` and checks the header.

**`render_template_string`, not a template directory.** `build_exe.py`
documents that the frozen build ships no template or data files. Adding a
template directory would change the packaging contract for a feature that does
not need it.

**Money as strings.** Prices are `str`, never `float`. Binary floats lose
cents, and this is a store.

---

## 5. Adding a real provider (7digital, Bandcamp, Qobuz, Juno Download)

The seam is `online_store/providers/base.py`. A provider implements:

```python
class MyStore(StoreProvider):
    provider_id = "my_store"
    display_name = "My Store"
    supports_purchase = True

    def search(self, query, limit=25): ...          # -> [Album]
    def list_albums(self, limit=50, offset=0): ...  # -> [Album]
    def get_album(self, album_id): ...              # -> Album | None
    def get_track(self, track_id): ...              # -> Track | None
    def purchase(self, track_id): ...               # -> Purchase | None
```

then registers with `@register_provider` and sets `provider = my_store` in
`online_store.ini`. **Nothing else in the subsystem changes** — not the
storefront, not the ServiceInfo document, not the installer, not the WMP URLs.

The contract is deliberately small: every method is something a real licensed
store API can answer. DRM licence vending, a synchronous cart on WMP's behalf,
and burning on WMP's initiative are all absent, because a real provider
cannot implement them either. Returning `None` or raising `ProviderError` is a
correct, supported outcome.

A provider **must not** scrape a website, work around an API's authentication
or rate limits, implement or strip DRM, or fetch anything the user is not
entitled to. This constraint is stated in `base.py` because it is the whole
reason this subsystem can be pointed at a commercial store at all.

---

## 6. What is deliberately not done

* **[TODO] No COM plug-in.** Not needed for a commerce store (section 1). The
  installer has a `--register-plugin-dll` path for a future music store.
* **[TODO] No `ActiveService` write.** Microsoft documents it as written *by
  WMP* when the user activates a store. The installer does not impersonate the
  user.
* **[TODO] No group policy.** `Software\Policies\Microsoft\WindowsMediaPlayer`
  is not a per-user feature toggle and is not used as one.
* **[TODO] No `AlbumInfo` / `BuyCD` / `InfoCenter`.** Microsoft's matrix marks
  all three "ignored" for a commerce store. Emitting elements a consumer is
  documented to ignore produces a document nobody has verified against
  anything.
* **[TODO] Not proven against a live WMP.** See section 3. Everything this
  project controls is tested; WMP's actual response to it is not.

---

## 7. Troubleshooting

Run the store's own diagnostics first — they answer most of this:

```powershell
python "FAI Server.py" online-store-status
Invoke-RestMethod http://127.0.0.1/online-store/api/status | ConvertTo-Json -Depth 6
```

| Symptom | Most likely cause |
|---|---|
| No Online Stores tab at all | `TestParameter` missing, or WMP not restarted after install. WMP reads the store list at startup. |
| Tab present, page blank | WMP fetched a ServiceInfo document whose `ServiceTask1` URL it cannot reach. Check `fai_server.log` for `[REQ] /online-store/serviceinfo.xml`. |
| That `[REQ]` never appears | The `BASEURL` guess is wrong, or the store is not in `TestParameter`. Try the documented `/DefaultService` route (section 3). |
| `503` from every store page | `enabled = false` in `online_store.ini`, or a config error. The 503 body names the file. |
| `502` | The provider raised `ProviderError`. The message is in the response body and in the log. |
| Install says it needs Administrator | The `HKLM` half of the store registration does. The `HKCU` half does not; `--dry-run` shows the whole plan without writing. |

To confirm the registry actually holds what was intended:

```powershell
reg query "HKLM\SOFTWARE\Microsoft\MediaPlayer\Subscriptions\legacy_music_store"
reg query "HKCU\Software\Microsoft\MediaPlayer\Services\legacy_music_store"
reg query "HKCU\Software\Microsoft\MediaPlayer\Services" /v TestParameter
```

## 8. References

Microsoft Learn, archived Windows Media Player SDK documentation
(`previous-versions/windows/desktop/wmp/`):

* Windows Media Player Online Stores — the feature matrix
* Registry Keys and Entries for a Type 2 Online Store
* ServiceInfo Document / Example ServiceInfo Document for a Type 2 Online Store
* Service Task Panes
* Type 2 Online Store Plug-in
* Test and Production Keys for a Type 2 Online Store
* Setup Command-line Parameters for Online Stores
* External Object for Type 2 Online Stores

Every page carries a banner marking the Windows Media Player SDK as a legacy
feature superseded by `Windows.Media.Playback`. That is worth keeping in mind:
this subsystem targets a client Microsoft no longer develops, which is why the
distinction between documented and verified behaviour matters so much here.


