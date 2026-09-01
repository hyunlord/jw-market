# 2026-09-01 Production Source Snapshot

This branch contains a source snapshot collected from the production deployments on 2026-09-01.

## Deployment Lineage

- Chat: revision 1786, commit `4ddf988eab45ada6d3ac120e3e268751185247b7`, image digest `sha256:cb6f4bed21b964da3735788ce6d6a6dfb91bc28f226a5516ca4508f5092b87da`
- Portal frontend: revision 232, commit `5b22fe1fe7273e5874d67fec05f91cea251a6d1b`, image digest `sha256:afb1d1551de40afd90cf7b9c37c5fce25bae6429d0e3b457ea644c6cb904cdb6`
- Portal backend: revision 92, commit `2b0c57a86aaf0385f4b08acf3e4c7a681a873392`, image digest `sha256:23498bf98bb3811d53764591c14849b5e4ee88fe30a484ae3187eae721b8080d`

Four backend credential values in `module-api/src/main/resources/application.yaml` were replaced with the explicit `${PORTAL_API_KEY}` placeholder. All other component files are unchanged from the 2026-09-01 deployed-source snapshot.

The snapshot is published as one commit on top of the existing `develop` history.
