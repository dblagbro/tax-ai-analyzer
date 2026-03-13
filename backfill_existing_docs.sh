#!/bin/bash
# Backfill existing tax documents from the legacy tax folder into Paperless consume directory
# Usage: ./backfill_existing_docs.sh [--dry-run]

SOURCE_DIR="/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax"
CONSUME_DIR="/mnt/s/documents/tax-organizer/consume"
DRY_RUN=false
YEARS_FILTER=""  # e.g. "2020 2021 2022" — empty means all years

for arg in "$@"; do
    [[ "$arg" == "--dry-run" ]] && DRY_RUN=true && echo "[DRY RUN MODE]"
    [[ "$arg" =~ ^--years=(.+)$ ]] && YEARS_FILTER="${BASH_REMATCH[1]}"
done

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "ERROR: Source directory not found: $SOURCE_DIR"
    exit 1
fi

copied=0
skipped=0

# Walk all year directories under SOURCE_DIR
for year_dir in "$SOURCE_DIR"/*/; do
    year=$(basename "$year_dir")
    if ! [[ "$year" =~ ^[0-9]{4}$ ]]; then
        echo "Skipping non-year directory: $year"
        continue
    fi
    # Filter to specific years if --years= was specified
    if [[ -n "$YEARS_FILTER" ]]; then
        match=false
        for y in $YEARS_FILTER; do [[ "$year" == "$y" ]] && match=true && break; done
        if [[ "$match" == false ]]; then
            echo "Skipping year (not in filter): $year"
            continue
        fi
    fi

    dest_year_dir="$CONSUME_DIR/personal/$year"
    echo "Processing year: $year → $dest_year_dir"

    if [[ "$DRY_RUN" == false ]]; then
        mkdir -p "$dest_year_dir"
    fi

    # Find all PDFs recursively in this year folder
    while IFS= read -r -d '' pdf; do
        basename_pdf=$(basename "$pdf")

        # Try to preserve YYYY_MM_DD_description-cost.pdf naming convention
        # If already in that format, keep it; otherwise rename with date prefix
        if [[ "$basename_pdf" =~ ^[0-9]{4}_[0-9]{2}_[0-9]{2}_ ]]; then
            dest_name="$basename_pdf"
        else
            # Prepend year_01_01_ if no date prefix
            safe_name=$(echo "$basename_pdf" | tr ' ' '_' | tr -dc '[:alnum:]._-')
            dest_name="${year}_01_01_${safe_name}"
        fi

        dest_path="$dest_year_dir/$dest_name"

        if [[ -f "$dest_path" ]]; then
            echo "  SKIP (exists): $dest_name"
            ((skipped++))
        else
            echo "  COPY: $basename_pdf → $dest_name"
            if [[ "$DRY_RUN" == false ]]; then
                cp "$pdf" "$dest_path"
            fi
            ((copied++))
        fi
    done < <(find "$year_dir" -name "*.pdf" -type f -print0)
done

echo ""
echo "============================="
echo "Backfill complete."
echo "  Copied:  $copied"
echo "  Skipped: $skipped"
if [[ "$DRY_RUN" == true ]]; then
    echo "  (Dry run — no files were actually copied)"
fi
echo ""
echo "Paperless consumer will pick up new files from: $CONSUME_DIR"
echo "Monitor progress: docker logs -f tax-paperless-consumer"
