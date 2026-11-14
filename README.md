# OpenCreator Pause Dock

Personal alpha for the Creator 5 Pro. Public for anyone who wants to try it.
Physical acceptance testing has not been recorded.

PAUSE docks the tool, turns its heater off and parks the carriage at X125/Y220.
RESUME reheats, picks up the tool and returns to the print.
Runout recovery, infinite spool and changing tools during pause are unsupported.
This does not replace power-loss recovery.

Install over SSH while the printer is idle:

```sh
oc-plugin install https://github.com/KidCe/opencreator-pause-dock.git
```

Update through Fluidd/Mainsail (`oc-plugin-pause-dock`) or:

```sh
oc-plugin update pause-dock
```

Disable: `oc-plugin disable pause-dock`. Settings: `config/pause-dock.cfg`.
Keep personal settings in a separate config override so Git updates stay clean.
Existing manually modified pause macros need to be restored before installing.

[Report a problem](https://github.com/KidCe/opencreator-pause-dock/issues): include
your OpenCreator/plugin version, what happened and relevant logs.

AI assistance: code, tests and documentation were produced with substantial
assistance from OpenAI Codex under the maintainer's direction.
