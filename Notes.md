1. Run the code in this project and identify all the warnings, errors, bugs and breakages in the system and fix them without any new bugs.

2. Perform full analysis and write documentations for the project in the root docs folder. The documents should include `README.md`, Product Requirements Document (`PRD.md`), Technical Requirements/Design Document (`TRD.md`), Project Tree (`PROJECT_TREE.md`), Deployment Guide (`DEPLOYMENT.md`), and any additional architecture, API, database schema, testing, security, and operational documentation required to ensure the project can be understood, maintained, deployed, and extended by any developer. Documentation must always remain synchronized with the implementation throughout development rather than being completed only at the end.

3. Perform a full vulnerability testing and identify all the vulnerabilities in the system. Fix all those vulnerabilities and also document them in the docs folder.

4. Prepare the project for full production deployment and provide all the necessary files needed for the deployment. Deployments will be done using docker on aws so provide the deployment guides as well and the needed steps to take during deployment.

---

---

1. Let the port be configured in the .env file so that the user can change the port if port 8000 is already in use by another service.

2. There are no database migration files in the codebase. Is this an expected workflow or there is an error. If it is an error, provide the necessary database migration files. Also provide admin seed cred and the user be able to set them in the env file.

3. Check the .env file and see if all vars are set. Docker is also running now so build the docker image and run all tests

---

---

Add pgAdmin front so the admin can login and access the data on the server. pgAdmin image is already on the server. Add a front so admins can login and access data.

---

---

1. Fix the admin event approval from the admin dashboard so that admins can approve newly created events before they are displayed in the home screen. Add a button to the actions for events pending approval.

2. All amount input fields can not be negative value as prices are suppose to be positive. Check all input field and make sure they don't enter a negative value.

---

---

1. Docker compose file was compromised in the last commits. Check and fix it to work as intended by the project.

2. ⁠Code Voting implementation to work like university elections with confirmation and submission messages. Create a secure voter-verification and ballot flow where each eligible voter is uniquely identified by their student/reference number and a one-time, securely generated voting token/ticket, verify that the voter is eligible for the specific election and has not already voted, display the voter’s basic verified profile before granting access to the ballot, present all available election positions and their eligible candidates with the correct voting rules (single choice, multiple choice, maximum selections, abstention where applicable), allow the voter to review their selections before final submission, require an explicit final confirmation, atomically record the vote so duplicate submissions, race conditions, refreshes, retries, or token reuse cannot result in multiple votes, immediately invalidate the voting token after a successful submission, prevent the system from storing unnecessary information that could link a voter’s identity to their individual ballot choices, maintain tamper-resistant audit logs for administrative actions and voting events without compromising ballot secrecy, provide administrators/election officials with appropriate controls for creating elections, managing positions and candidates, importing or verifying eligible voters, generating/resetting voting credentials according to strict authorization rules, opening and closing elections, monitoring aggregate turnout, and viewing/counting results only according to the configured election rules, and implement proper authorization, input validation, CSRF protection, rate limiting, secure token hashing, encryption where appropriate, transaction handling, database constraints, session security, error handling, and comprehensive tests; first inspect my existing codebase and architecture, identify the relevant models, routes, components, APIs, and database schema, then implement this as a native feature using the project’s existing conventions and technologies, clearly documenting any database migrations, environment variables, API changes, and setup steps required.

---

---

---

---

---

---

---

---

---

---

---

---

---

---
