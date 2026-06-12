# CLEAN HANDS v1.0 FINAL — one-repo consolidation

This is the EXACT state running in production (checksummed below). Applying it
changes nothing on the server — it makes GitHub main equal to it, forever.

In a claude.ai/code session on zentoshi69/CLEAN-HANDS, attach this tarball and
paste:

    Extract the attached tarball into clean-hands/ with --strip-components=1,
    run staking-api/test_staking.py to confirm the suite passes, commit as
    "v1.0 FINAL — locked production state" and push to main. Then create and
    push a git tag v1.0-final.

After that, the ONLY deploy command that exists is (on the server):

    cd /home/clean/CLEAN-HANDS && sudo -u clean git pull && sudo bash clean-hands/deploy/redeploy.sh
