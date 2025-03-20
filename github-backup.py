import os
import argparse
import logging
from github import Github
from git import Repo, GitCommandError

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

def clone_or_update_repo(clone_url, repo_path, dryrun=False):
    if dryrun:
        logging.info(f"DRYRUN: Repository URL: {clone_url}, Path: {repo_path}")
        return

    try:
        if os.path.exists(repo_path):
            logging.info(f"Updating {repo_path}...")
            repo = Repo(repo_path)
            origin = repo.remotes.origin

            # Fetch latest changes from the remote
            origin.fetch()

            # Determine default branch from the origin's HEAD symbolic ref
            try:
                # This will return something like "refs/remotes/origin/main"
                default_branch = repo.git.symbolic_ref("refs/remotes/origin/HEAD").split('/')[-1]
                logging.info(f"Default branch determined as: {default_branch}")
            except Exception as e:
                logging.error(f"Failed to determine default branch for {repo_path}: {str(e)}")
                default_branch = 'master'  # Fallback to 'master' if necessary

            # Checkout the default branch
            repo.git.checkout(default_branch)
            logging.info(f"Switched to default branch: {default_branch}")

            # Pull changes using rebase
            origin.pull(rebase=True)
            logging.info(f"Successfully updated {repo_path} with rebase pull")

            # Update submodules if .gitmodules exists
            gitmodules_path = os.path.join(repo_path, '.gitmodules')
            if os.path.exists(gitmodules_path):
                repo.git.submodule('update', '--init', '--recursive')
                logging.info(f"Submodules updated for {repo_path}")
            else:
                logging.info(f"No .gitmodules file found in {repo_path}; skipping submodule update.")
        else:
            logging.info(f"Cloning {repo_path}...")
            repo = Repo.clone_from(clone_url, repo_path)
            logging.info(f"Successfully cloned {repo_path}")

            # Initialize and update submodules if .gitmodules exists
            gitmodules_path = os.path.join(repo_path, '.gitmodules')
            if os.path.exists(gitmodules_path):
                repo.git.submodule('update', '--init', '--recursive')
                logging.info(f"Submodules initialized for {repo_path}")
            else:
                logging.info(f"No .gitmodules file found in {repo_path}; skipping submodule initialization.")
    except GitCommandError as e:
        logging.error(f"Failed to process {repo_path}: {str(e)}")

def fetch_repositories(github_token):
    g = Github(github_token)
    user = g.get_user()
    repos = user.get_repos()
    repo_list = []

    for repo in repos:
        repo_list.append({
            'name': repo.name,
            'clone_url': repo.ssh_url  # Use ssh_url for SSH cloning
        })
        logging.info(f"Found repository: {repo.name}")

    return repo_list

def clone_repositories(token_file, output_folder, dryrun=False):
    # Read the token from the file
    with open(token_file, 'r') as file:
        token = file.readline().strip()

    # Ensure output folder exists
    os.makedirs(output_folder, exist_ok=True)

    # Fetch repositories
    repos = fetch_repositories(token)

    if not repos:
        logging.info("No repositories found.")
    else:
        logging.info(f"Found {len(repos)} repositories.")

    for repo in repos:
        clone_url = repo['clone_url']
        repo_name = repo['name']
        repo_path = os.path.join(output_folder, repo_name)

        # Clone or update the repository
        clone_or_update_repo(clone_url, repo_path, dryrun)

if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Backup GitHub repositories.")
    parser.add_argument("token_file", type=str, help="Path to the file containing your GitHub personal access token")
    parser.add_argument("--output", "-o", type=str, default="repositories",
                        help="Output folder where repositories will be cloned (default: 'repositories')")
    parser.add_argument("--dryrun", action="store_true", help="List repositories without cloning them")

    args = parser.parse_args()

    # Call function to clone or update repositories
    clone_repositories(args.token_file, args.output, args.dryrun)
