1. Run the code in this project and identify all the warnings, errors, bugs and breakages in the system and fix them without any new bugs.

2. Perform full analysis and write documentations for the project in the root docs folder. The documents should include `README.md`, Product Requirements Document (`PRD.md`), Technical Requirements/Design Document (`TRD.md`), Project Tree (`PROJECT_TREE.md`), Deployment Guide (`DEPLOYMENT.md`), and any additional architecture, API, database schema, testing, security, and operational documentation required to ensure the project can be understood, maintained, deployed, and extended by any developer. Documentation must always remain synchronized with the implementation throughout development rather than being completed only at the end.

3. Perform a full vulnerability testing and identify all the vulnerabilities in the system. Fix all those vulnerabilities and also document them in the docs folder.

4. Prepare the project for full production deployment and provide all the necessary files needed for the deployment. Deployments will be done using docker on aws so provide the deployment guides as well and the needed steps to take during deployment.

---

---

1. Let the port be configured in the .env file so that the user can change the port is port 8000 is already in use by another service.

2. There are no database migration files in the codebase. Is this an expected workflow or there is an error. If it is an error, provide the necessary database migration files. Also provide admin seed cred and the user be able to set them in the env file.

3. Check the .env file and see if all vars are set. Docker is also running now so build the docker image and run all tests
