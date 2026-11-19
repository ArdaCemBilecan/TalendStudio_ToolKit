# The point-and-click interface (`tmt ui`)

It is a small local web page, so there is nothing to
install and nothing leaves your computer.

```console
$ tmt ui
tmt interface running. Open this address if your browser does not open by itself:
http://127.0.0.1:52144/?token=...
```

Your browser opens by itself. Press **Ctrl+C** in the terminal (or the **Stop tmt** button) to stop it.

## Using it: the Dependency Explorer

The window is laid out like Talend Studio, so it feels familiar next to it.

| Area | What it is |
|---|---|
| **Title bar** | The **Workspace** folder: type or paste it, or press **Browse...**. Sub-folders (every project) are searched. |
| **Repository** (left) | **Job Designs**: master jobs first (jobs that start others), then the rest. Open a master with a click; the arrow shows the jobs it starts, click one to find it in the graph. **Db Connections**: the metadata connections used in the current view; click one to light up the jobs that use it. The filter box narrows the list. |
| **Designer** (centre) | One tab per opened job (close with the x). The graph: the job, the jobs it starts (however deep), the tables and files they read and write. Hover (or Tab to) a node for its numbers, click to keep them open, Esc to clear. |
| **Bottom panel** | **Sources**, **Targets** and **Connections** as lists. Click a table to find it in the graph. |
| **Properties** (right) | Distinct sources and targets of the whole workflow, the connections by name (`10x ODS_DB`, `2x AKY_DB`), and the details of the node you selected. |
| **Toolbar** | Zoom, *Fit to window*, *Save as SVG* (the picture you are looking at), and *Show jobs that share tables* (neighbours outside the workflow). |

A name with `*` was recognised from context variables only (Studio names them after the metadata connection),
not from an explicit link: an inference, shown as one. Light and dark follow your system setting; on a phone the
repository folds behind a button.

No jobs at hand? *Open the demo project* in the Designer.

The page remembers the folder and the choices you used (in your browser only).

## Good to know

* **Output goes to the "Output folder"** (default `out`, relative to where you started `tmt ui`). Files there are
  overwritten on every run. The result says the full path and has a *Show output folder* button.
* **Large projects can take a while.** The button says it is running; a run is stopped after ten minutes.
* **One command at a time.** Starting a second while one is running is refused with a message.
* **Mistakes get a sentence, not a stack trace:** a folder that does not exist, an unknown option, no jobs in the
  folder.
* It works with the keyboard (Tab, arrow keys on the action list, Enter in the folder box, Esc closes the folder
  picker), at phone width, and in light and dark mode. Status is always written in words, never colour alone.

## Security: what it is and is not

The server can list folders and read job files for whoever can talk to it, so it is deliberately narrow:

| Measure | Why |
|---|---|
| Listens on **127.0.0.1 only**, on a random port | not reachable from the network |
| Every request needs a long **random token** (it is in the address `tmt ui` prints) | another program on your machine, or a web page in another tab, cannot use it |
| `Host` must be the loopback address; `Origin`, if sent, must match | blocks DNS-rebinding and cross-site requests; no CORS headers are sent |
| **Read-only**: no routes that run commands, write files or serve files from disk (a test fails if the server module starts a process) | there is nothing to hijack beyond reading the folders you point it at |
| `nosniff`, `no-store`, and a strict Content-Security-Policy; the page is built with `textContent`, never `innerHTML` for job data | an untrusted job name cannot attack the page |

The folder picker lists folder names on your computer, but only to the page that has the token. Do not share the
address `tmt ui` prints: anyone who has it, on this computer, can browse folders and run the actions. Close the
tab and press Ctrl+C when you are done.

## Options

```text
tmt ui [--port N] [--no-browser]
```

`--port` picks a fixed port (default: any free one). `--no-browser` only prints the address.
