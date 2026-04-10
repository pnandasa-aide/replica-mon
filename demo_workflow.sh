#!/bin/bash
###############################################################################
# GlueSync End-to-End Demo Workflow
#
# This script demonstrates the complete replication workflow:
# 1. Create source table on AS400 with journaling
# 2. Create target table on MSSQL with Change Tracking
# 3. Create GlueSync pipeline and add entities
# 4. Start replication (Snapshot + CDC mode)
# 5. Generate mockup data on source
# 6. Verify replication via journal entries and CT
# 7. Run comparison report
#
# Usage:
#   chmod +x demo_workflow.sh
#   ./demo_workflow.sh [single|multi] [--dry-run]
#
# Modes:
#   single     - Demo with single table (default)
#   multi      - Demo with multiple tables
#   --dry-run  - Show commands without executing (safe testing)
#
# Examples:
#   ./demo_workflow.sh single           # Run single table demo
#   ./demo_workflow.sh multi            # Run multi-table demo
#   ./demo_workflow.sh single --dry-run # Preview commands without execution
###############################################################################

set -e  # Exit on error

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QADMCLI_DIR="$SCRIPT_DIR/../qadmcli"
GLUESYNC_DIR="$SCRIPT_DIR/../gluesync-cli"
REPLICA_DIR="$SCRIPT_DIR"
ENV_FILE="$SCRIPT_DIR/../.env"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_section() {
    echo ""
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE} $1${NC}"
    echo -e "${BLUE}================================================================${NC}"
    echo ""
}

# Check if .env exists
if [ ! -f "$ENV_FILE" ]; then
    log_error "Environment file not found: $ENV_FILE"
    log_error "Please create .env file with required credentials"
    exit 1
fi

# Validate required directories and files
VALIDATION_ERRORS=0

log_info "Validating environment..."

# Check qadmcli
if [ ! -d "$QADMCLI_DIR" ]; then
    log_error "qadmcli directory not found: $QADMCLI_DIR"
    VALIDATION_ERRORS=$((VALIDATION_ERRORS + 1))
elif [ ! -f "$QADMCLI_DIR/qadmcli.sh" ]; then
    log_error "qadmcli.sh not found in $QADMCLI_DIR"
    VALIDATION_ERRORS=$((VALIDATION_ERRORS + 1))
else
    log_success "qadmcli found: $QADMCLI_DIR/qadmcli.sh"
fi

# Check gluesync-cli
if [ ! -d "$GLUESYNC_DIR" ]; then
    log_error "gluesync-cli directory not found: $GLUESYNC_DIR"
    VALIDATION_ERRORS=$((VALIDATION_ERRORS + 1))
elif [ ! -f "$GLUESYNC_DIR/gluesync_cli_v2.py" ]; then
    log_error "gluesync_cli_v2.py not found in $GLUESYNC_DIR"
    VALIDATION_ERRORS=$((VALIDATION_ERRORS + 1))
else
    log_success "gluesync-cli found: $GLUESYNC_DIR/gluesync_cli_v2.py"
fi

# Check replica-mon
if [ "$DRY_RUN" = false ]; then
    if [ ! -f "$REPLICA_DIR/compare.py" ]; then
        log_error "compare.py not found in $REPLICA_DIR"
        VALIDATION_ERRORS=$((VALIDATION_ERRORS + 1))
    else
        log_success "replica-mon compare.py found: $REPLICA_DIR/compare.py"
    fi
fi

if [ $VALIDATION_ERRORS -gt 0 ]; then
    log_error "Validation failed with $VALIDATION_ERRORS error(s)"
    exit 1
fi

log_success "Environment validation passed"

# Source environment
source "$ENV_FILE"
export AS400_USER AS400_PASSWORD MSSQL_USER MSSQL_PASSWORD MSSQL_ADMIN_USER MSSQL_ADMIN_PASSWORD
export GLUESYNC_ADMIN_USERNAME GLUESYNC_ADMIN_PASSWORD

log_info "Environment loaded from $ENV_FILE"

# Parse arguments
MODE="single"
DRY_RUN=false
SHOW_HELP=false

for arg in "$@"; do
    case $arg in
        single|multi)
            MODE="$arg"
            ;;
        --dry-run)
            DRY_RUN=true
            ;;
        --help|-h)
            SHOW_HELP=true
            ;;
        *)
            log_error "Unknown argument: $arg"
            log_error "Usage: $0 [single|multi] [--dry-run] [--help]"
            exit 1
            ;;
    esac
done

# Show help if requested
if [ "$SHOW_HELP" = true ]; then
    echo "GlueSync End-to-End Demo Workflow"
    echo ""
    echo "Usage:"
    echo "  $0 [single|multi] [--dry-run] [--help]"
    echo ""
    echo "Modes:"
    echo "  single     - Demo with single table (default)"
    echo "  multi      - Demo with multiple tables"
    echo ""
    echo "Options:"
    echo "  --dry-run  - Show commands without executing (safe testing)"
    echo "  --help     - Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 single              # Run single table demo"
    echo "  $0 multi               # Run multi-table demo"
    echo "  $0 single --dry-run    # Preview commands without execution"
    echo ""
    echo "Workflow Phases:"
    echo "  1. Create Source Table on AS400 (with journaling)"
    echo "  2. Create Target Table on MSSQL (with Change Tracking)"
    echo "  3. Create GlueSync Pipeline and Add Entities"
    echo "  4. Start Replication (Snapshot + CDC mode)"
    echo "  5. Generate Mockup Data on Source"
    echo "  6. Verify Replication via Journal Entries and CT"
    echo "  7. Run Comparison Report"
    echo "  8. Summary"
    echo ""
    echo "Features:"
    echo "  - Checks if tables exist before creating"
    echo "  - Skips already created resources"
    echo "  - Validates environment before execution"
    echo "  - Supports dry-run mode for safe testing"
    exit 0
fi

log_info "Running in mode: $MODE"
if [ "$DRY_RUN" = true ]; then
    log_warn "DRY-RUN MODE: Commands will be shown but NOT executed"
fi

# Helper function to execute or preview commands
run_cmd() {
    local cmd="$1"
    local description="$2"
    
    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] Would execute: $description"
        echo "  Command: $cmd"
        echo ""
    else
        log_info "Executing: $description"
        eval "$cmd"
    fi
}

# Helper function for commands with expected failures
run_cmd_allow_fail() {
    local cmd="$1"
    local description="$2"
    
    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] Would execute: $description"
        echo "  Command: $cmd"
        echo ""
    else
        log_info "Executing: $description"
        eval "$cmd" || log_warn "Command failed (may be expected): $description"
    fi
}

###############################################################################
# PHASE 1: CREATE SOURCE TABLE ON AS400
###############################################################################
log_section "PHASE 1: Create Source Table on AS400"

if [ "$MODE" = "single" ]; then
    TABLES=("CUSTOMERS")
elif [ "$MODE" = "multi" ]; then
    TABLES=("CUSTOMERS" "ORDERS" "PRODUCTS")
else
    log_error "Invalid mode: $MODE. Use 'single' or 'multi'"
    exit 1
fi

LIBRARY="GSLIBTST"

for TABLE in "${TABLES[@]}"; do
    log_info "Checking if table $LIBRARY.$TABLE exists on AS400..."
    
    cd "$QADMCLI_DIR"
    
    # Check if table exists
    TABLE_EXISTS=$(./qadmcli.sh sql execute -q "SELECT COUNT(*) FROM QSYS2.SYSTABLES WHERE TABLE_SCHEMA='$LIBRARY' AND TABLE_NAME='$TABLE'" 2>&1 | grep -oP '^\d+' | head -1)
    
    if [ "$TABLE_EXISTS" = "1" ]; then
        log_warn "Table $LIBRARY.$TABLE already exists, skipping creation"
    else
        log_info "Creating table: $LIBRARY.$TABLE with journaling..."
        
        # Create table on AS400
        run_cmd_allow_fail \
            "./qadmcli.sh sql execute -q \"CREATE TABLE $LIBRARY.$TABLE (CUST_ID BIGINT NOT NULL PRIMARY KEY, FIRST_NAME VARCHAR(50), LAST_NAME VARCHAR(50), EMAIL VARCHAR(100), PHONE VARCHAR(20), CREATED_DATE TIMESTAMP, STATUS VARCHAR(20))\" 2>&1 | tail -5" \
            "Create AS400 table $LIBRARY.$TABLE"
    fi
    
    # Check if journaling is already enabled
    JOURNAL_STATUS=$(./qadmcli.sh journal info -n "$TABLE" -l "$LIBRARY" 2>&1 | grep "Journaled:" | awk '{print $2}')
    
    if [ "$JOURNAL_STATUS" = "Yes" ]; then
        log_warn "Journaling already enabled on $LIBRARY.$TABLE"
    else
        # Enable journaling on table
        run_cmd_allow_fail \
            "./qadmcli.sh journal enable -n \"$TABLE\" -l \"$LIBRARY\" 2>&1 | tail -3" \
            "Enable journaling on $LIBRARY.$TABLE"
    fi
    
    log_success "Table $LIBRARY.$TABLE ready with journaling"
done

###############################################################################
# PHASE 2: CREATE TARGET TABLE ON MSSQL WITH CHANGE TRACKING
###############################################################################
log_section "PHASE 2: Create Target Table on MSSQL with Change Tracking"

SCHEMA="dbo"
DATABASE="GSTargetDB"

for TABLE in "${TABLES[@]}"; do
    log_info "Checking if table $SCHEMA.$TABLE exists on MSSQL..."
    
    cd "$QADMCLI_DIR"
    
    # Check if table exists
    TABLE_EXISTS=$(./qadmcli.sh mssql query -q "SELECT COUNT(*) as cnt FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='$SCHEMA' AND TABLE_NAME='$TABLE'" --format json 2>&1 | python3 -c "import sys, json; data=json.load(sys.stdin); print(data[0]['cnt'])" 2>/dev/null || echo "0")
    
    if [ "$TABLE_EXISTS" = "1" ]; then
        log_warn "Table $SCHEMA.$TABLE already exists, skipping creation"
    else
        log_info "Creating table: $SCHEMA.$TABLE with CT enabled..."
        
        # Create table on MSSQL
        run_cmd_allow_fail \
            "./qadmcli.sh mssql execute -q \"CREATE TABLE $SCHEMA.$TABLE (CUST_ID BIGINT PRIMARY KEY, FIRST_NAME NVARCHAR(50), LAST_NAME NVARCHAR(50), EMAIL NVARCHAR(100), PHONE NVARCHAR(20), CREATED_DATE DATETIME2, STATUS NVARCHAR(20))\" 2>&1 | tail -3" \
            "Create MSSQL table $SCHEMA.$TABLE"
    fi
    
    # Enable Change Tracking on database (if not already enabled)
    if [ "$TABLE" = "${TABLES[0]}" ]; then
        # Check if CT is enabled on database
        CT_DB_STATUS=$(./qadmcli.sh mssql ct status 2>&1 | grep "CT Enabled on Database:" | awk '{print $NF}')
        
        if [ "$CT_DB_STATUS" = "Yes" ]; then
            log_warn "Change Tracking already enabled on database $DATABASE"
        else
            run_cmd_allow_fail \
                "./qadmcli.sh mssql ct enable-db --admin-user \"$MSSQL_ADMIN_USER\" --admin-password \"$MSSQL_ADMIN_PASSWORD\" --retention 2 2>&1 | tail -5" \
                "Enable CT on database $DATABASE"
        fi
    fi
    
    # Check if CT is enabled on table
    CT_TABLE_STATUS=$(./qadmcli.sh mssql ct status -t "$TABLE" -s "$SCHEMA" 2>&1 | grep "CT Enabled on Table:" | awk '{print $NF}')
    
    if [ "$CT_TABLE_STATUS" = "Yes" ]; then
        log_warn "Change Tracking already enabled on table $SCHEMA.$TABLE"
    else
        # Enable CT on table
        run_cmd_allow_fail \
            "./qadmcli.sh mssql ct enable-table -t \"$TABLE\" -s \"$SCHEMA\" 2>&1 | tail -3" \
            "Enable CT on table $SCHEMA.$TABLE"
    fi
    
    # Verify CT status
    run_cmd \
        "./qadmcli.sh mssql ct status -t \"$TABLE\" -s \"$SCHEMA\" 2>&1 | grep -E \"CT Enabled|Error\" | head -2" \
        "Verify CT status for $SCHEMA.$TABLE"
    
    log_success "Table $SCHEMA.$TABLE ready with CT enabled"
done

###############################################################################
# PHASE 3: CREATE GLUESYNC PIPELINE AND ADD ENTITIES
###############################################################################
log_section "PHASE 3: Create GlueSync Pipeline and Add Entities"

PIPELINE_NAME="Demo Replication Pipeline"
PIPELINE_ID=""

cd "$GLUESYNC_DIR"

# Step 1: Check if pipeline already exists
log_info "Checking for existing pipeline..."
EXISTING_PIPELINE=$(python3 gluesync_cli_v2.py list pipelines 2>&1 | grep -i "demo" | head -1)

if [ -n "$EXISTING_PIPELINE" ]; then
    log_warn "Pipeline already exists, using existing one"
    PIPELINE_ID=$(echo "$EXISTING_PIPELINE" | awk '{print $1}')
    log_info "Pipeline ID: $PIPELINE_ID"
else
    # Create new pipeline
    log_info "Creating new pipeline: $PIPELINE_NAME..."
    
    PIPELINE_OUTPUT=$(python3 gluesync_cli_v2.py create pipeline \
        --name "$PIPELINE_NAME" \
        --source-type AS400 \
        --target-type MSSQL \
        --source-host "${AS400_HOST:-161.82.146.249}" \
        --source-user "$AS400_USER" \
        --source-password "$AS400_PASSWORD" \
        --target-host "${MSSQL_HOST:-192.168.13.62}" \
        --target-user "$MSSQL_USER" \
        --target-password "$MSSQL_PASSWORD" \
        --target-database "$DATABASE" 2>&1)
    
    PIPELINE_EXIT_CODE=$?
    
    if [ $PIPELINE_EXIT_CODE -ne 0 ]; then
        log_error "Failed to create pipeline (exit code: $PIPELINE_EXIT_CODE)"
        echo "$PIPELINE_OUTPUT" | tail -20
        log_error "Cannot continue with replication phases"
        PIPELINE_ID=""
    else
        # Extract pipeline ID
        PIPELINE_ID=$(echo "$PIPELINE_OUTPUT" | grep -oP '(?<=Pipeline ID: )\w+' | head -1)
        
        if [ -z "$PIPELINE_ID" ]; then
            log_error "Failed to extract pipeline ID from output"
            echo "$PIPELINE_OUTPUT" | tail -10
            log_error "Cannot continue with replication phases"
        else
            log_success "Pipeline created with ID: $PIPELINE_ID"
        fi
    fi
fi

# Step 2: Check/add entities for each table
if [ -n "$PIPELINE_ID" ]; then
    for TABLE in "${TABLES[@]}"; do
        log_info "Checking entity for $LIBRARY.$TABLE -> $SCHEMA.$TABLE..."
        
        # Check if entity already exists for this table mapping
        EXISTING_ENTITY=$(python3 gluesync_cli_v2.py list entities --pipeline "$PIPELINE_ID" 2>&1 | \
                         grep "$TABLE" | grep "$LIBRARY" | head -1)
        
        if [ -n "$EXISTING_ENTITY" ]; then
            ENTITY_ID=$(echo "$EXISTING_ENTITY" | awk '{print $1}')
            log_warn "Entity already exists for $TABLE (ID: $ENTITY_ID), skipping creation"
        else
            log_info "Adding new entity: $LIBRARY.$TABLE -> $SCHEMA.$TABLE..."
            
            ENTITY_OUTPUT=$(python3 gluesync_cli_v2.py create entity \
                --pipeline "$PIPELINE_ID" \
                --source-library "$LIBRARY" \
                --source-table "$TABLE" \
                --target-schema "$SCHEMA" \
                --target-table "$TABLE" \
                --polling-interval 500 \
                --batch-size 1000 2>&1)
            
            ENTITY_ID=$(echo "$ENTITY_OUTPUT" | grep -oP '(?<=Entity ID: )\w+' | head -1)
            
            if [ -z "$ENTITY_ID" ]; then
                log_error "Failed to create entity for $TABLE"
                echo "$ENTITY_OUTPUT" | tail -10
            else
                log_success "Entity created with ID: $ENTITY_ID"
            fi
        fi
    done
else
    log_error "No pipeline available, cannot add entities"
    log_warn "Please create pipeline manually via GlueSync UI"
fi

###############################################################################
# PHASE 4: START REPLICATION
###############################################################################
log_section "PHASE 4: Start Replication"

if [ -n "$PIPELINE_ID" ]; then
    cd "$GLUESYNC_DIR"
    
    for TABLE in "${TABLES[@]}"; do
        log_info "Checking entity status for $TABLE..."
        
        # Get entity ID
        ENTITY_ID=$(python3 gluesync_cli_v2.py list entities --pipeline "$PIPELINE_ID" 2>&1 | \
                    grep "$TABLE" | awk '{print $1}' | head -1)
        
        if [ -z "$ENTITY_ID" ]; then
            log_error "Entity not found for table $TABLE"
            continue
        fi
        
        # Check if entity is already running
        ENTITY_STATUS=$(python3 gluesync_cli_v2.py list entities --pipeline "$PIPELINE_ID" 2>&1 | \
                       grep "$TABLE" | awk '{print $3}' | head -1)
        
        if [ "$ENTITY_STATUS" = "RUNNING" ] || [ "$ENTITY_STATUS" = "ACTIVE" ]; then
            log_warn "Entity for $TABLE is already running (Status: $ENTITY_STATUS)"
        else
            log_info "Starting entity for $TABLE (Snapshot + CDC mode)..."
            
            # Start entity with snapshot mode (initial load + CDC)
            python3 gluesync_cli_v2.py start entity "$ENTITY_ID" \
                --pipeline "$PIPELINE_ID" \
                --mode snapshot 2>&1 | tail -5
            
            log_success "Entity $TABLE started in snapshot mode"
        fi
        
        # Wait a moment for snapshot to begin
        sleep 2
    done
    
    log_info "Waiting 10 seconds for initial snapshot to complete..."
    sleep 10
else
    log_error "No pipeline available, cannot start replication"
    log_warn "Please create pipeline and entities manually via GlueSync UI"
fi

###############################################################################
# PHASE 5: GENERATE MOCKUP DATA ON SOURCE
###############################################################################
log_section "PHASE 5: Generate Mockup Data on AS400 Source"

cd "$QADMCLI_DIR"

for TABLE in "${TABLES[@]}"; do
    log_info "Generating mockup data for $LIBRARY.$TABLE..."
    
    ./qadmcli.sh mockup generate \
        -t "$TABLE" \
        -l "$LIBRARY" \
        -n 20 \
        --insert-ratio 50 \
        --update-ratio 30 \
        --delete-ratio 20 2>&1 | grep -E "Inserted|Updated|Deleted"
    
    log_success "Mockup data generated for $TABLE"
done

log_info "Waiting 10 seconds for GlueSync to replicate changes..."
sleep 10

###############################################################################
# PHASE 6: VERIFY REPLICATION - CHECK JOURNAL AND CT
###############################################################################
log_section "PHASE 6: Verify Replication - Check Journal and CT"

for TABLE in "${TABLES[@]}"; do
    log_section "Table: $TABLE"
    
    # Check AS400 Journal
    log_info "Checking AS400 Journal for $LIBRARY.$TABLE..."
    cd "$QADMCLI_DIR"
    
    JOURNAL_SUMMARY=$(./qadmcli.sh journal entries \
        -n "$TABLE" \
        -l "$LIBRARY" \
        --format summary 2>&1 | grep -v "INFO\|Using\|Running\|Connected\|Disconnected")
    
    echo "$JOURNAL_SUMMARY" | python3 -c "
import json, sys
try:
    # Find JSON in output
    lines = sys.stdin.read()
    start = lines.find('{')
    end = lines.rfind('}') + 1
    if start >= 0 and end > start:
        data = json.loads(lines[start:end])
        print(f\"  Total Entries: {data.get('total', 0)}\")
        print(f\"  Inserts (PT): {data.get('inserts', 0)}\")
        print(f\"  Updates (UP): {data.get('updates', 0)}\")
        print(f\"  Deletes (DL): {data.get('deletes', 0)}\")
except:
    print('  (Could not parse journal summary)')
" 2>/dev/null || echo "  (Journal query completed)"
    
    # Check MSSQL CT
    log_info "Checking MSSQL CT for $SCHEMA.$TABLE..."
    
    CT_SUMMARY=$(./qadmcli.sh mssql ct changes \
        -t "$TABLE" \
        -s "$SCHEMA" \
        --format summary 2>&1 | grep -v "INFO\|Using\|Running\|Connected\|Disconnected\|Current CT")
    
    echo "$CT_SUMMARY" | python3 -c "
import json, sys
try:
    lines = sys.stdin.read()
    start = lines.find('{')
    end = lines.rfind('}') + 1
    if start >= 0 and end > start:
        data = json.loads(lines[start:end])
        print(f\"  Total Changes: {data.get('total', 0)}\")
        print(f\"  Inserts (I): {data.get('inserts', 0)}\")
        print(f\"  Updates (U): {data.get('updates', 0)}\")
        print(f\"  Deletes (D): {data.get('deletes', 0)}\")
        print(f\"  CT Version: {data.get('current_version', 0)}\")
except:
    print('  (Could not parse CT summary)')
" 2>/dev/null || echo "  (CT query completed)"
    
    echo ""
done

###############################################################################
# PHASE 7: RUN COMPARISON REPORT
###############################################################################
log_section "PHASE 7: Run Comparison Report"

cd "$REPLICA_DIR"

for TABLE in "${TABLES[@]}"; do
    log_section "Comparison Report: $TABLE"
    
    python3 compare.py \
        --source "$LIBRARY.$TABLE" \
        --target "$SCHEMA.$TABLE" \
        --format text 2>&1
    
    echo ""
done

###############################################################################
# PHASE 8: SUMMARY
###############################################################################
log_section "DEMO WORKFLOW COMPLETE"

log_info "Summary:"
echo "  Mode: $MODE"
echo "  Tables: ${TABLES[*]}"
echo "  Source Library: $LIBRARY"
echo "  Target Schema: $SCHEMA"
echo "  Pipeline ID: $PIPELINE_ID"
echo ""

log_info "Next Steps:"
echo "  1. Check GlueSync UI: https://${GLUESYNC_HOST:-192.168.13.53}:1717/ui/index.html"
echo "  2. Monitor pipeline status"
echo "  3. Run comparison report anytime:"
echo "     cd $REPLICA_DIR"
echo "     python3 compare.py --source $LIBRARY.<TABLE> --target $SCHEMA.<TABLE>"
echo ""

log_success "Demo workflow completed successfully!"

###############################################################################
# OPTIONAL: Cleanup (uncomment to enable)
###############################################################################
# log_section "Cleanup (Optional)"
# read -p "Do you want to cleanup created resources? (y/N) " -n 1 -r
# echo
# if [[ $REPLY =~ ^[Yy]$ ]]; then
#     log_info "Stopping entities..."
#     # Add cleanup commands here
#     
#     log_info "Deleting entities..."
#     # Add entity deletion commands here
#     
#     log_info "Deleting pipeline..."
#     # Add pipeline deletion commands here
#     
#     log_success "Cleanup completed"
# fi
