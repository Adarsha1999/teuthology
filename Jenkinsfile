pipeline {
    agent any
    
    parameters {
        string(name: 'OVERRIDE_YAML', defaultValue: '/home/ubuntu/override.yaml', description: 'Path to override YAML file')
    }
    
    options {
        timestamps()
        timeout(time: 24, unit: 'HOURS')
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }
    
    environment {
        SCRIPT_DIR = '/home/ubuntu/teuthology'
        LOG_DIR = "${SCRIPT_DIR}/logs"
    }
    
    stages {
        stage('Initialize') {
            steps {
                script {
                    env.LOG_FILE = "${LOG_DIR}/daily-smoke-${env.BUILD_NUMBER}-${new Date().format('yyyyMMdd-HHmmss')}.log"
                    sh "mkdir -p ${LOG_DIR}"
                    dir(env.SCRIPT_DIR) {
                        sh "pwd"
                    }
                }
            }
        }
        
        stage('Determine Branches') {
            steps {
                script {
                    def calendar = Calendar.getInstance()
                    def dayOfWeek = calendar.get(Calendar.DAY_OF_WEEK)
                    // Convert to ISO day of week (1=Monday, 7=Sunday)
                    def isoDayOfWeek = dayOfWeek == Calendar.SUNDAY ? 7 : dayOfWeek - 1
                    def dayName = new Date().format('EEEE')
                    
                    env.RUN_TENTACLE = (isoDayOfWeek == 1 || isoDayOfWeek == 3) ? 'true' : 'false'
                    env.DAY_NAME = dayName
                    
                    echo "[${new Date().format('yyyy-MM-dd HH:mm:ss')}] Today is ${dayName} - ${env.RUN_TENTACLE == 'true' ? 'will run both tentacle and main branches' : 'will run main branch only (tentacle runs only on Monday and Wednesday)'}"
                }
            }
        }
        
        stage('Run Smoke Suite') {
            steps {
                script {
                    if (env.RUN_TENTACLE == 'true') {
                        log("Starting smoke suite for 'tentacle' branch...")
                        log("")
                        if (runSmokeForBranch('tentacle')) {
                            log("✓ Smoke suite for 'tentacle' branch completed")
                        } else {
                            log("✗ Smoke suite for 'tentacle' branch had errors")
                        }
                        log("")
                        log("Starting smoke suite for 'main' branch (tentacle run has completed)...")
                        log("")
                        if (runSmokeForBranch('main')) {
                            log("✓ Smoke suite for 'main' branch completed")
                        } else {
                            log("✗ Smoke suite for 'main' branch had errors")
                        }
                    } else {
                        log("Starting smoke suite for 'main' branch...")
                        log("")
                        if (runSmokeForBranch('main')) {
                            log("✓ Smoke suite for 'main' branch completed")
                        } else {
                            log("✗ Smoke suite for 'main' branch had errors")
                        }
                    }
                    log("")
                    log("==========================================")
                    log("Daily smoke suite execution completed")
                    log("==========================================")
                }
            }
        }
    }
    
    post {
        always {
            script {
                if (fileExists(env.LOG_FILE)) {
                    archiveArtifacts artifacts: "${env.LOG_FILE}", allowEmptyArchive: true
                }
            }
        }
        success {
            echo "Daily smoke suite execution completed successfully"
        }
        failure {
            echo "Daily smoke suite execution completed with failures"
        }
    }
}

def log(message) {
    def timestamp = new Date().format('yyyy-MM-dd HH:mm:ss')
    def logMessage = "[${timestamp}] ${message}"
    echo logMessage
    sh "echo '${logMessage}' >> ${env.LOG_FILE}"
}

def constructRunName(suite, timestamp, cephBranch) {
    def user = sh(script: 'whoami', returnStdout: true).trim()
    def kernelBranch = "distro"
    def worker = "openstack"
    def flavor = "default"
    
    suite = suite.replaceAll("/", ":")
    return "${user}-${timestamp}-${suite}-${cephBranch}-${kernelBranch}-${flavor}-${worker}"
}

def runSmokeForBranch(branch) {
    dir(env.SCRIPT_DIR) {
        log("Starting smoke suite for branch: ${branch}")
        
        // Get shaman_id for the branch
        def shamanId
        try {
            shamanId = sh(
                script: """
                    python3 getUpstreamBuildDetails.py \\
                        --branch ${branch} \\
                        --platform ubuntu-jammy-default,centos-9-default \\
                        --arch x86_64
                """,
                returnStdout: true
            ).trim()
            
            if (!shamanId) {
                log("ERROR: Failed to get upstream build details for branch ${branch}")
                currentBuild.result = 'UNSTABLE'
                return false
            }
            
            log("Using shaman build id for branch ${branch}: ${shamanId}")
        } catch (Exception e) {
            log("ERROR: Exception getting shaman_id for branch ${branch}: ${e.message}")
            currentBuild.result = 'UNSTABLE'
            return false
        }
        
        // Upload shaman_id to remote server
        try {
            def dateStr = sh(script: 'date "+%Y-%m-%d"', returnStdout: true).trim()
            sh """
                sshpass -p "admin" ssh -o StrictHostKeyChecking=no cloud-user@10.0.196.233 \\
                    "sudo mkdir -p /data/scheduler/cron && echo '${shamanId}' | sudo tee /data/scheduler/cron/${branch}-${dateStr} > /dev/null"
            """
        } catch (Exception e) {
            log("WARNING: Failed to upload shaman_id: ${e.message}")
        }
        
        // Unlock targets before running
        log("Unlocking targets...")
        try {
            sh """
                teuthology-lock --list-targets --owner scheduled_ubuntu@teuth-teuthology > ~/locked_targets 2>> ${env.LOG_FILE} || true
            """
        } catch (Exception e) {
            log("WARNING: Failed to list targets, continuing anyway...")
        }
        
        try {
            sh """
                teuthology-lock --owner scheduled_ubuntu@teuth-teuthology --unlock -t ~/locked_targets -vvv >> ${env.LOG_FILE} 2>&1 || true
            """
        } catch (Exception e) {
            log("WARNING: Failed to unlock targets, continuing anyway...")
        }
        
        // Smoke suite configuration
        def suite = "smoke"
        def seed = 8446
        
        log("Starting smoke suite for branch ${branch} with seed=${seed}")
        
        // Capture timestamp
        def timestamp = sh(script: 'date "+%Y-%m-%d_%H:%M:%S"', returnStdout: true).trim()
        
        // Build and run command
        def cmd = """
            teuthology-suite \\
                --suite "${suite}" \\
                --machine-type openstack \\
                --ceph "${branch}" \\
                --ceph-repo https://github.com/ceph/ceph \\
                --priority 50 \\
                --force-priority \\
                --seed ${seed} \\
                --sha1 ${shamanId} \\
                ${params.OVERRIDE_YAML}
        """
        
        log("Running command: ${cmd}")
        
        // Execute teuthology-suite and capture output
        def suiteOutput
        try {
            def outputFile = "${env.WORKSPACE}/suite_output_${branch}_${System.currentTimeMillis()}.txt"
            def exitCode = sh(
                script: """
                    ${cmd} > ${outputFile} 2>&1
                    cat ${outputFile} >> ${env.LOG_FILE}
                    cat ${outputFile}
                    exit \${PIPESTATUS[0]}
                """,
                returnStatus: true
            )
            
            // Read the output for parsing
            suiteOutput = readFile(file: outputFile)
            sh "rm -f ${outputFile}"
            
            if (exitCode != 0) {
                log("ERROR: Failed to schedule smoke suite for branch ${branch}")
                currentBuild.result = 'UNSTABLE'
                return false
            }
        } catch (Exception e) {
            log("ERROR: Exception running teuthology-suite: ${e.message}")
            currentBuild.result = 'UNSTABLE'
            return false
        }
        
        // Extract run name from teuthology-suite output
        def runName
        def matcher = suiteOutput =~ /Job scheduled with name (\S+)/
        if (matcher.find()) {
            runName = matcher.group(1)
        } else {
            log("WARNING: Could not extract run name from teuthology-suite output, constructing it...")
            runName = constructRunName(suite, timestamp, branch)
        }
        
        log("Using run name: ${runName}")
        
        // Wait for run to be registered
        log("Waiting 10 seconds for run to be registered on server...")
        sleep(time: 10, unit: 'SECONDS')
        
        // Verify run exists
        log("Verifying run exists on server...")
        def runExists = false
        def maxAttempts = 6
        def attempt = 0
        
        while (attempt < maxAttempts) {
            try {
                def exitCode = sh(
                    script: """
                        python3 <<EOF
from teuthology.report import ResultsReporter
try:
    reporter = ResultsReporter()
    jobs = reporter.get_jobs('${runName}', fields=['job_id'])
    if jobs is not None:
        exit(0)
except Exception as e:
    if '404' in str(e) or 'Not Found' in str(e):
        exit(1)
    exit(0)
EOF
                    """,
                    returnStatus: true
                )
                
                if (exitCode == 0) {
                    runExists = true
                    break
                }
            } catch (Exception e) {
                // Continue trying
            }
            
            attempt++
            if (attempt < maxAttempts) {
                log("Run not found yet, waiting 5 seconds... (attempt ${attempt}/${maxAttempts})")
                sleep(time: 5, unit: 'SECONDS')
            }
        }
        
        if (!runExists) {
            log("WARNING: Could not verify run '${runName}' exists after ${maxAttempts} attempts.")
            log("Will attempt to wait anyway - teuthology-wait will handle this.")
        } else {
            log("Run verified on server: ${runName}")
        }
        
        // Wait for suite to complete using teuthology-wait
        log("Waiting for smoke suite (branch: ${branch}, run: ${runName}) to complete using teuthology-wait...")
        try {
            def exitCode = sh(
                script: "teuthology-wait --run ${runName} >> ${env.LOG_FILE} 2>&1",
                returnStatus: true
            )
            
            if (exitCode != 0) {
                log("WARNING: Smoke suite for branch ${branch} completed with failures or errors")
                currentBuild.result = 'UNSTABLE'
                return false
            } else {
                log("✓ Smoke suite for branch ${branch} completed successfully (teuthology-wait confirmed completion)")
                return true
            }
        } catch (Exception e) {
            log("ERROR: Exception waiting for suite: ${e.message}")
            currentBuild.result = 'UNSTABLE'
            return false
        }
    }
}
