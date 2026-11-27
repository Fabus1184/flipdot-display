# 3D models

Scripts that gave the Rev 2.1 footprints their 3D models (2026-11-25).

- **Stock models.** Most parts use models from KiCad's official 3D library,
  referenced as `${KICAD10_3DMODEL_DIR}/…`. They show up in any KiCad
  installation that has the 3D library (on Arch: the `kicad-library-3d`
  package).
- **Placement.** [`fit.py`](fit.py) finds each model's rotation and offset. It
  fits the matching KiCad stock footprint onto the EasyEDA footprint:
  - by pad number where the numbers correspond, which keeps polarity and pin 1
    right;
  - otherwise by pad geometry.

  The result is in [`fit.json`](fit.json).
- **Attaching.** [`apply_models.py`](apply_models.py) writes the models into the
  project library and the board.
- **Bundled models.** Two parts have no public model, so
  [`custom_models.py`](custom_models.py) generates them into
  `kicad/flipdot-driver/flipdot.3dshapes/`, referenced as `${KIPRJMOD}/…`:
  - the 16 mm toroid inductor (L1);
  - the 30-pin ESP32 DevKit body (DOIT V1 size) that sits in the U5 sockets.

  The DevKit's sockets, ESP32-WROOM module, micro-USB socket and regulator are
  stock KiCad models.

```bash
python3 fit.py hardware/kicad/flipdot-driver fit.json
python3 custom_models.py hardware/kicad/flipdot-driver/flipdot.3dshapes
python3 apply_models.py hardware/kicad/flipdot-driver fit.json
```

**Approximations:**

| Part | Model used | Difference from the real part |
| --- | --- | --- |
| USB1 | GCT USB4105 (16-pin) | Close match, not the exact SHOU HAN part |
| L1 | Generic wound toroid on a header base | Generic shape; the Pulse PE-53117NL has no public model |
| D23 (MBRD1045) | TO-252-2 | Correct package family |
| U5 | Simplified DevKit | Rough stand-in for the real module |
