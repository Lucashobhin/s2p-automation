"""One-off: add the two CHECK constraints Alembic's autogenerate cannot detect.

Run from backend/ right after generating the 'create rfq ...' migration:
    python scripts\\patch_migration.py
"""
import glob
import sys

files = glob.glob("alembic/versions/*create_rfq*.py")
if len(files) != 1:
    sys.exit(f"ABORT: expected exactly 1 migration file matching *create_rfq*, found {len(files)}: {files}")
path = files[0]
text = open(path, encoding="utf-8").read()

up_old = "        batch_op.add_column(sa.Column('ordered_quantity', sa.Integer(), server_default='0', nullable=False))\n"
up_new = up_old + (
    "        batch_op.create_check_constraint('ck_pr_ordered_nonneg', 'ordered_quantity >= 0')\n"
    "        batch_op.create_check_constraint('ck_pr_ordered_within_quantity', 'ordered_quantity <= quantity')\n"
)
down_old = "        batch_op.drop_column('ordered_quantity')\n"
down_new = (
    "        batch_op.drop_constraint('ck_pr_ordered_within_quantity', type_='check')\n"
    "        batch_op.drop_constraint('ck_pr_ordered_nonneg', type_='check')\n"
) + down_old

if "create_check_constraint" in text:
    sys.exit("ABORT: already patched")
for name, old in (("upgrade line", up_old), ("downgrade line", down_old)):
    if text.count(old) != 1:
        sys.exit(f"ABORT: {name} not found exactly once; paste the migration file to me.")
text = text.replace(up_old, up_new).replace(down_old, down_new)
open(path, "w", encoding="utf-8").write(text)
print("patched:", path)