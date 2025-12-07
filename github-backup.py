"""
GitHub Repository Backup Tool

This script automates the backup of GitHub repositories by cloning new repositories
and updating existing ones. It supports submodules, dry-run mode, configuration files,
and provides comprehensive logging.
"""

import os
import sys
import argparse
import logging
import time
import json
from pathlib import Path
from typing import List, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from github import Github, GithubException, RateLimitExceededException, Auth
from git import Repo, GitCommandError
from tqdm import tqdm

# Constants
DEFAULT_OUTPUT_FOLDER = "repositories"
DEFAULT_GISTS_FOLDER = "gists"
DEFAULT_BRANCH_FALLBACK = "main"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"


@dataclass
class RepositoryInfo:
    """Data class for repository information."""
    name: str
    clone_url: str
    is_private: bool = False
    is_fork: bool = False
    is_archived: bool = False


@dataclass
class GistInfo:
    """Data class for gist information."""
    id: str
    description: str
    clone_url: str
    is_public: bool = True


@dataclass
class BackupStats:
    """Statistics for the backup operation."""
    total: int = 0
    cloned: int = 0
    updated: int = 0
    failed: int = 0
    skipped: int = 0
    gists_total: int = 0
    gists_cloned: int = 0
    gists_updated: int = 0
    gists_failed: int = 0


@dataclass
class Config:
    """Configuration for the backup operation."""
    token_file: str
    output_folder: str = DEFAULT_OUTPUT_FOLDER
    verbose: bool = False
    dryrun: bool = False
    max_retries: int = MAX_RETRIES
    retry_delay_seconds: int = RETRY_DELAY_SECONDS
    use_ssh: bool = True
    backup_gists: bool = True
    exclude_forks: bool = False
    exclude_archived: bool = False
    include_only: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    log_file: str = ""

    @classmethod
    def from_file(cls, config_path: str) -> 'Config':
        """Load configuration from JSON file."""
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        github_config = data.get('github', {})
        backup_config = data.get('backup', {})
        filters = data.get('filters', {})

        return cls(
            token_file=github_config.get('token_file', 'token.txt'),
            output_folder=backup_config.get('output_folder', DEFAULT_OUTPUT_FOLDER),
            verbose=backup_config.get('verbose', False),
            max_retries=backup_config.get('max_retries', MAX_RETRIES),
            retry_delay_seconds=backup_config.get('retry_delay_seconds', RETRY_DELAY_SECONDS),
            use_ssh=github_config.get('use_ssh', True),
            backup_gists=backup_config.get('backup_gists', True),
            exclude_forks=filters.get('exclude_forks', False),
            exclude_archived=filters.get('exclude_archived', False),
            include_only=filters.get('include_only', []),
            exclude=filters.get('exclude', []),
            log_file=backup_config.get('log_file', '')
        )


def setup_logging(verbose: bool = False, log_file: str = "", quiet_console: bool = False) -> logging.Logger:
    """
    Configure logging with appropriate level and format.
    Sets up both console and file logging if log_file is provided.

    Args:
        verbose: If True, set logging level to DEBUG
        log_file: Path to log file (optional)
        quiet_console: If True, only show WARNING and above on console (for progress bars)

    Returns:
        Configured logger instance
    """
    level = logging.DEBUG if verbose else logging.INFO
    handlers = []

    # Console handler with simple format
    console_handler = logging.StreamHandler(sys.stdout)
    # If quiet_console is enabled, only show WARNING and above to avoid interfering with progress bars
    console_handler.setLevel(logging.WARNING if quiet_console else level)
    console_formatter = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)
    console_handler.setFormatter(console_formatter)
    handlers.append(console_handler)

    # File handler with detailed format if log_file specified
    if log_file:
        file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)  # Always log everything to file
        file_formatter = logging.Formatter(LOG_FILE_FORMAT, LOG_DATE_FORMAT)
        file_handler.setFormatter(file_formatter)
        handlers.append(file_handler)

    logging.basicConfig(
        level=logging.DEBUG if log_file else level,
        handlers=handlers,
        force=True  # Force reconfiguration
    )

    logger = logging.getLogger(__name__)
    if log_file and not quiet_console:
        logger.info(f"Logging to file: {log_file}")

    return logger


def read_token(token_file: str) -> str:
    """
    Read GitHub token from file or environment variable.

    Args:
        token_file: Path to file containing the token

    Returns:
        GitHub personal access token

    Raises:
        FileNotFoundError: If token file doesn't exist
        ValueError: If token is empty
    """
    # Check environment variable first
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        logging.info("Using GitHub token from GITHUB_TOKEN environment variable")
        return token.strip()

    # Read from file
    token_path = Path(token_file)
    if not token_path.exists():
        raise FileNotFoundError(f"Token file not found: {token_file}")

    with open(token_path, 'r', encoding='utf-8') as file:
        token = file.readline().strip()

    if not token:
        raise ValueError("Token file is empty")

    logging.info(f"Using GitHub token from file: {token_file}")
    return token


def check_rate_limit(github_client: Github) -> None:
    """
    Check and log GitHub API rate limit status.

    Args:
        github_client: Authenticated GitHub client instance
    """
    try:
        rate_limit = github_client.get_rate_limit()
        core = rate_limit.core
        logging.debug(f"API Rate Limit - Remaining: {core.remaining}/{core.limit}, "
                     f"Reset at: {core.reset}")

        if core.remaining < 10:
            logging.warning(f"Low API rate limit remaining: {core.remaining}")
            logging.warning(f"Rate limit resets at: {core.reset}")
    except Exception as e:
        logging.debug(f"Could not check rate limit: {e}")


def fetch_repositories(github_token: str, config: Config) -> List[RepositoryInfo]:
    """
    Fetch all repositories accessible by the authenticated user.

    Args:
        github_token: GitHub personal access token
        config: Configuration object with filter settings

    Returns:
        List of RepositoryInfo objects

    Raises:
        GithubException: If authentication fails or API error occurs
    """
    logger = logging.getLogger(__name__)
    logger.info("Authenticating with GitHub...")

    try:
        auth = Auth.Token(github_token)
        github_client = Github(auth=auth)
        user = github_client.get_user()

        logger.info(f"Authenticated as: {user.login}")
        check_rate_limit(github_client)

        logger.info("Fetching repositories...")
        repos = user.get_repos()
        repo_list = []

        for repo in repos:
            # Apply filters
            if config.exclude_forks and repo.fork:
                logger.debug(f"Skipping fork: {repo.name}")
                continue

            if config.exclude_archived and repo.archived:
                logger.debug(f"Skipping archived: {repo.name}")
                continue

            if config.include_only and repo.name not in config.include_only:
                logger.debug(f"Skipping (not in include_only): {repo.name}")
                continue

            if config.exclude and repo.name in config.exclude:
                logger.debug(f"Skipping (in exclude list): {repo.name}")
                continue

            # Choose URL based on config
            clone_url = repo.ssh_url if config.use_ssh else repo.clone_url

            repo_info = RepositoryInfo(
                name=repo.name,
                clone_url=clone_url,
                is_private=repo.private,
                is_fork=repo.fork,
                is_archived=repo.archived
            )
            repo_list.append(repo_info)
            logger.debug(f"Found repository: {repo.name} "
                        f"(Private: {repo.private}, Fork: {repo.fork}, "
                        f"Archived: {repo.archived})")

        logger.info(f"Successfully fetched {len(repo_list)} repositories")
        return repo_list

    except RateLimitExceededException:
        logger.error("GitHub API rate limit exceeded")
        raise
    except GithubException as e:
        logger.error(f"GitHub API error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while fetching repositories: {e}")
        raise


def fetch_gists(github_token: str) -> List[GistInfo]:
    """
    Fetch all gists for the authenticated user.

    Args:
        github_token: GitHub personal access token

    Returns:
        List of GistInfo objects

    Raises:
        GithubException: If authentication fails or API error occurs
    """
    logger = logging.getLogger(__name__)
    logger.info("Fetching gists...")

    try:
        auth = Auth.Token(github_token)
        github_client = Github(auth=auth)
        user = github_client.get_user()

        gists = user.get_gists()
        gist_list = []

        for gist in gists:
            description = gist.description or f"gist-{gist.id}"
            gist_info = GistInfo(
                id=gist.id,
                description=description,
                clone_url=gist.git_pull_url,
                is_public=gist.public
            )
            gist_list.append(gist_info)
            logger.debug(f"Found gist: {gist.id} - {description} "
                        f"(Public: {gist.public})")

        logger.info(f"Successfully fetched {len(gist_list)} gists")
        return gist_list

    except GithubException as e:
        logger.error(f"GitHub API error while fetching gists: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while fetching gists: {e}")
        raise


def get_default_branch(repo: Repo, repo_name: str) -> str:
    """
    Determine the default branch of a repository.

    Args:
        repo: GitPython Repo object
        repo_name: Name of the repository for logging

    Returns:
        Name of the default branch
    """
    logger = logging.getLogger(__name__)

    try:
        # Try to get the default branch from origin HEAD
        symbolic_ref = repo.git.symbolic_ref("refs/remotes/origin/HEAD")
        default_branch = symbolic_ref.split('/')[-1]
        logger.debug(f"[{repo_name}] Default branch: {default_branch}")
        return default_branch
    except Exception as e:
        logger.warning(f"[{repo_name}] Could not determine default branch: {e}")
        logger.warning(f"[{repo_name}] Falling back to '{DEFAULT_BRANCH_FALLBACK}'")
        return DEFAULT_BRANCH_FALLBACK


def has_uncommitted_changes(repo: Repo) -> bool:
    """
    Check if repository has uncommitted changes.

    Args:
        repo: GitPython Repo object

    Returns:
        True if there are uncommitted changes
    """
    return repo.is_dirty(untracked_files=True)


def update_repository(repo_path: Path, repo_name: str, progress_bar=None) -> bool:
    """
    Update an existing repository.

    Args:
        repo_path: Path to the repository
        repo_name: Name of the repository for logging
        progress_bar: Optional tqdm progress bar for updates

    Returns:
        True if update was successful, False otherwise
    """
    logger = logging.getLogger(__name__)

    if progress_bar:
        progress_bar.set_description(f"Updating {repo_name}")

    logger.info(f"[{repo_name}] Updating existing repository...")

    try:
        if progress_bar:
            progress_bar.update(10)  # Starting update

        repo = Repo(str(repo_path))

        # Check for uncommitted changes
        if has_uncommitted_changes(repo):
            logger.warning(f"[{repo_name}] Repository has uncommitted changes, "
                          "skipping update to avoid conflicts")
            return False

        if progress_bar:
            progress_bar.update(10)  # Checked status

        origin = repo.remotes.origin

        # Fetch latest changes
        logger.debug(f"[{repo_name}] Fetching from origin...")
        fetch_info = origin.fetch()
        logger.debug(f"[{repo_name}] Fetched {len(fetch_info)} refs")

        if progress_bar:
            progress_bar.update(30)  # Fetched changes

        # Get and checkout default branch
        default_branch = get_default_branch(repo, repo_name)
        current_branch = repo.active_branch.name

        if current_branch != default_branch:
            logger.info(f"[{repo_name}] Switching from '{current_branch}' "
                       f"to '{default_branch}'")
            repo.git.checkout(default_branch)

        if progress_bar:
            progress_bar.update(10)  # Checked out branch

        # Pull changes with rebase
        logger.debug(f"[{repo_name}] Pulling changes with rebase...")
        pull_info = origin.pull(rebase=True)

        if pull_info:
            logger.info(f"[{repo_name}] Successfully updated "
                       f"({pull_info[0].commit.hexsha[:7]})")
        else:
            logger.info(f"[{repo_name}] Already up to date")

        if progress_bar:
            progress_bar.update(20)  # Pulled changes

        # Update submodules if present
        gitmodules_path = repo_path / ".gitmodules"
        if gitmodules_path.exists():
            if progress_bar:
                progress_bar.set_description(f"Updating submodules for {repo_name}")
            logger.debug(f"[{repo_name}] Updating submodules...")
            repo.git.submodule("update", "--init", "--recursive")
            logger.info(f"[{repo_name}] Submodules updated")
            if progress_bar:
                progress_bar.update(20)  # Updated submodules
        else:
            if progress_bar:
                progress_bar.update(20)  # No submodules

        return True

    except GitCommandError as e:
        logger.error(f"[{repo_name}] Git command failed: {e}")
        return False
    except Exception as e:
        logger.error(f"[{repo_name}] Unexpected error during update: {e}")
        return False


def clone_repository(clone_url: str, repo_path: Path, repo_name: str, max_retries: int = MAX_RETRIES, progress_bar=None) -> bool:
    """
    Clone a new repository with retry logic.

    Args:
        clone_url: SSH or HTTPS URL to clone from
        repo_path: Destination path for the repository
        repo_name: Name of the repository for logging
        max_retries: Maximum number of retry attempts
        progress_bar: Optional tqdm progress bar for updates

    Returns:
        True if clone was successful, False otherwise
    """
    logger = logging.getLogger(__name__)

    if progress_bar:
        progress_bar.set_description(f"Cloning {repo_name}")

    logger.info(f"[{repo_name}] Cloning new repository...")

    for attempt in range(1, max_retries + 1):
        try:
            if progress_bar:
                progress_bar.update(10)  # Starting clone

            repo = Repo.clone_from(clone_url, str(repo_path))
            logger.info(f"[{repo_name}] Successfully cloned")

            if progress_bar:
                progress_bar.update(50)  # Cloned repository

            # Initialize submodules if present
            gitmodules_path = repo_path / ".gitmodules"
            if gitmodules_path.exists():
                if progress_bar:
                    progress_bar.set_description(f"Initializing submodules for {repo_name}")
                logger.debug(f"[{repo_name}] Initializing submodules...")
                repo.git.submodule("update", "--init", "--recursive")
                logger.info(f"[{repo_name}] Submodules initialized")
                if progress_bar:
                    progress_bar.update(40)  # Initialized submodules
            else:
                if progress_bar:
                    progress_bar.update(40)  # No submodules

            return True

        except GitCommandError as e:
            logger.error(f"[{repo_name}] Git command failed (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                logger.info(f"[{repo_name}] Retrying in {RETRY_DELAY_SECONDS} seconds...")
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                return False
        except Exception as e:
            logger.error(f"[{repo_name}] Unexpected error during clone: {e}")
            return False

    return False


def process_repository(
    repo_info: RepositoryInfo,
    output_folder: Path,
    config: Config,
    progress_bar=None
) -> Tuple[bool, str]:
    """
    Process a single repository (clone or update) with retry logic.

    Args:
        repo_info: Repository information
        output_folder: Base folder for repositories
        config: Configuration object
        progress_bar: Optional tqdm progress bar for updates

    Returns:
        Tuple of (success: bool, action: str)
    """
    logger = logging.getLogger(__name__)
    repo_path = output_folder / repo_info.name

    if config.dryrun:
        action = "update" if repo_path.exists() else "clone"
        logger.info(f"[DRYRUN] Would {action}: {repo_info.name} "
                   f"-> {repo_path}")
        return True, "skipped"

    try:
        if repo_path.exists():
            success = update_repository(repo_path, repo_info.name, progress_bar)
            return success, "updated" if success else "failed"
        else:
            success = clone_repository(clone_url=repo_info.clone_url,
                                      repo_path=repo_path,
                                      repo_name=repo_info.name,
                                      max_retries=config.max_retries,
                                      progress_bar=progress_bar)
            return success, "cloned" if success else "failed"

    except Exception as e:
        logger.error(f"[{repo_info.name}] Unexpected error: {e}")
        return False, "failed"


def process_gist(
    gist_info: GistInfo,
    gists_folder: Path,
    config: Config,
    progress_bar=None
) -> Tuple[bool, str]:
    """
    Process a single gist (clone or update).

    Args:
        gist_info: Gist information
        gists_folder: Base folder for gists
        config: Configuration object
        progress_bar: Optional tqdm progress bar for updates

    Returns:
        Tuple of (success: bool, action: str)
    """
    logger = logging.getLogger(__name__)
    gist_path = gists_folder / gist_info.id

    if config.dryrun:
        action = "update" if gist_path.exists() else "clone"
        logger.info(f"[DRYRUN] Would {action} gist: {gist_info.id} - {gist_info.description}")
        return True, "skipped"

    try:
        if gist_path.exists():
            success = update_repository(gist_path, f"gist:{gist_info.id}", progress_bar)
            return success, "updated" if success else "failed"
        else:
            success = clone_repository(clone_url=gist_info.clone_url,
                                      repo_path=gist_path,
                                      repo_name=f"gist:{gist_info.id}",
                                      max_retries=config.max_retries,
                                      progress_bar=progress_bar)
            return success, "cloned" if success else "failed"
    except Exception as e:
        logger.error(f"[gist:{gist_info.id}] Unexpected error: {e}")
        return False, "failed"


def print_summary(stats: BackupStats, duration: float) -> None:
    """
    Print summary statistics of the backup operation.

    Args:
        stats: BackupStats object with operation statistics
        duration: Duration of the operation in seconds
    """
    print("=" * 60)
    print("BACKUP SUMMARY")
    print("=" * 60)
    print("Repositories:")
    print(f"  Total:              {stats.total}")
    print(f"  Cloned:             {stats.cloned}")
    print(f"  Updated:            {stats.updated}")
    print(f"  Failed:             {stats.failed}")
    print(f"  Skipped:            {stats.skipped}")
    if stats.gists_total > 0:
        print("Gists:")
        print(f"  Total:              {stats.gists_total}")
        print(f"  Cloned:             {stats.gists_cloned}")
        print(f"  Updated:            {stats.gists_updated}")
        print(f"  Failed:             {stats.gists_failed}")
    print(f"Duration:             {duration:.2f} seconds")
    print("=" * 60)

    # Also log to file
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("BACKUP SUMMARY")
    logger.info("=" * 60)
    logger.info("Repositories:")
    logger.info(f"  Total:              {stats.total}")
    logger.info(f"  Cloned:             {stats.cloned}")
    logger.info(f"  Updated:            {stats.updated}")
    logger.info(f"  Failed:             {stats.failed}")
    logger.info(f"  Skipped:            {stats.skipped}")
    if stats.gists_total > 0:
        logger.info("Gists:")
        logger.info(f"  Total:              {stats.gists_total}")
        logger.info(f"  Cloned:             {stats.gists_cloned}")
        logger.info(f"  Updated:            {stats.gists_updated}")
        logger.info(f"  Failed:             {stats.gists_failed}")
    logger.info(f"Duration:             {duration:.2f} seconds")
    logger.info("=" * 60)


def backup_repositories(config: Config) -> int:
    """
    Main function to backup all GitHub repositories and gists.

    Args:
        config: Configuration object

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    # Generate log file name if not specified
    if not config.log_file:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        config.log_file = f"github_backup_{timestamp}.log"

    # Initial setup with console output
    logger = setup_logging(config.verbose, config.log_file, quiet_console=False)
    start_time = time.time()
    stats = BackupStats()

    try:
        # Read token
        token = read_token(config.token_file)

        # Create output folder
        output_path = Path(config.output_folder)
        output_path.mkdir(parents=True, exist_ok=True)
        print(f"Output folder: {output_path.absolute()}")

        # Fetch repositories
        repositories = fetch_repositories(token, config)
        stats.total = len(repositories)

        if not repositories:
            print("No repositories found")
        else:
            # Switch to quiet console mode for progress bars
            setup_logging(config.verbose, config.log_file, quiet_console=True)

            print(f"\nBacking up {stats.total} repositories...\n")

            for idx, repo_info in enumerate(repositories, 1):
                # Create a fresh progress bar for each repository
                with tqdm(total=100, desc=f"[{idx}/{stats.total}] {repo_info.name[:45]}",
                         unit="%", ncols=100, leave=False,
                         bar_format='{desc}: {bar}| {n_fmt}%') as pbar:

                    success, action = process_repository(repo_info, output_path, config, pbar)

                    if action == "cloned":
                        stats.cloned += 1
                    elif action == "updated":
                        stats.updated += 1
                    elif action == "skipped":
                        stats.skipped += 1
                    elif action == "failed":
                        stats.failed += 1

                # Show a summary line after each repo
                status_icon = "✓" if action in ["cloned", "updated"] else "✗" if action == "failed" else "○"
                print(f"{status_icon} [{idx}/{stats.total}] {repo_info.name} - {action}")

            print()  # Empty line after all repos

            # Re-enable console logging for summary
            setup_logging(config.verbose, config.log_file, quiet_console=False)

        # Backup gists if enabled
        if config.backup_gists:
            gists_path = output_path / DEFAULT_GISTS_FOLDER
            gists_path.mkdir(parents=True, exist_ok=True)

            try:
                gists = fetch_gists(token)
                stats.gists_total = len(gists)

                if not gists:
                    print("No gists found")
                else:
                    # Switch to quiet console mode for progress bars
                    setup_logging(config.verbose, config.log_file, quiet_console=True)

                    print(f"\nBacking up {stats.gists_total} gists...\n")

                    for idx, gist_info in enumerate(gists, 1):
                        desc_short = gist_info.description[:40] if gist_info.description else gist_info.id

                        # Create a fresh progress bar for each gist
                        with tqdm(total=100, desc=f"[{idx}/{stats.gists_total}] {desc_short}",
                                 unit="%", ncols=100, leave=False,
                                 bar_format='{desc}: {bar}| {n_fmt}%') as pbar:

                            success, action = process_gist(gist_info, gists_path, config, pbar)

                            if action == "cloned":
                                stats.gists_cloned += 1
                            elif action == "updated":
                                stats.gists_updated += 1
                            elif action == "failed":
                                stats.gists_failed += 1

                        # Show a summary line after each gist
                        status_icon = "✓" if action in ["cloned", "updated"] else "✗" if action == "failed" else "○"
                        print(f"{status_icon} [{idx}/{stats.gists_total}] {desc_short} - {action}")

                    print()  # Empty line after all gists

                    # Re-enable console logging
                    setup_logging(config.verbose, config.log_file, quiet_console=False)
            except Exception as e:
                # Re-enable console logging for error
                setup_logging(config.verbose, config.log_file, quiet_console=False)
                logger = logging.getLogger(__name__)
                logger.error(f"Error backing up gists: {e}")

        # Print summary
        duration = time.time() - start_time
        print("\n")
        print_summary(stats, duration)

        return 0 if (stats.failed == 0 and stats.gists_failed == 0) else 1

    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        return 1
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1
    except GithubException as e:
        logger.error(f"GitHub API error: {e}")
        return 1
    except KeyboardInterrupt:
        logger.warning("\nOperation cancelled by user")
        return 130
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=config.verbose)
        return 1


def main() -> None:
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Backup GitHub repositories by cloning or updating them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Backup all repositories using token from file
  %(prog)s token.txt

  # Backup to custom folder
  %(prog)s token.txt -o /backup/github

  # Dry run to see what would be done
  %(prog)s token.txt --dryrun

  # Use token from environment variable
  export GITHUB_TOKEN=your_token_here
  %(prog)s token.txt

  # Verbose output for debugging
  %(prog)s token.txt -v

  # Use configuration file
  %(prog)s --config config.json
        """
    )

    parser.add_argument(
        "token_file",
        type=str,
        nargs='?',
        help="Path to file containing GitHub personal access token "
             "(or set GITHUB_TOKEN environment variable)"
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        help="Path to JSON configuration file"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        help=f"Output folder for repositories (default: {DEFAULT_OUTPUT_FOLDER})"
    )
    parser.add_argument(
        "--dryrun",
        action="store_true",
        help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose debug logging"
    )
    parser.add_argument(
        "--exclude-forks",
        action="store_false",
        help="Exclude forked repositories"
    )
    parser.add_argument(
        "--exclude-archived",
        action="store_false",
        help="Exclude archived repositories"
    )
    parser.add_argument(
        "--no-gists",
        action="store_false",
        help="Skip backing up gists"
    )
    parser.add_argument(
        "--log-file", "-l",
        type=str,
        help="Path to log file (default: auto-generated with timestamp)"
    )

    args = parser.parse_args()

    # Load configuration
    if args.config:
        try:
            config = Config.from_file(args.config)
        except Exception as e:
            print(f"Error loading configuration file: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        if not args.token_file:
            parser.error("token_file is required when not using --config")

        config = Config(
            token_file=args.token_file,
            output_folder=args.output or DEFAULT_OUTPUT_FOLDER,
            verbose=args.verbose,
            dryrun=args.dryrun,
            backup_gists=not args.no_gists,
            exclude_forks=args.exclude_forks,
            exclude_archived=args.exclude_archived,
            log_file=args.log_file or ""
        )

    # Override config with command-line arguments if provided
    if args.output:
        config.output_folder = args.output
    if args.verbose:
        config.verbose = True
    if args.dryrun:
        config.dryrun = True
    if args.exclude_forks:
        config.exclude_forks = True
    if args.exclude_archived:
        config.exclude_archived = True
    if args.no_gists:
        config.backup_gists = False
    if args.log_file:
        config.log_file = args.log_file

    # Run backup and exit with appropriate code
    exit_code = backup_repositories(config)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
