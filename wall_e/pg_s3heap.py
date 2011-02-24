"""A program to do an inconsistent backup of a PostgreSQL heap to S3,
hopefully quickly.  This is done without putting the heap into one
file (via tar or cpio) because in addition to multi-stream S3 PUT, it
is also important to be able to parallelize GET, and one convenient
way to do that is send the Postgres heap as-is, as a bunch of files,
none of which are thought to substantially exceed 1GB.

General approach:

* Prerequisite: There exists an archive_command that is capturing
  WALs.  Forever.  If WAL segments need cleaning, then it should be
  possible to do so asyncronously.  This command does not grab a
  consistent snapshot of the heap.

* Call pg_start_backup on the live system

* Copy the heap to S3 (lzo compression applied in passing)

* Call pg_stop_backup on the live system

Anti-goals:

 * Take care of WAL segments in any way 

 * Perform any testing of the backup

"""

import argparse
import csv
import datetime
import multiprocessing
import os
import subprocess
import sys
import tempfile
import textwrap

PSQL_BIN = 'psql'
LZOP_BIN = 'lzop'
S3CMD_BIN = 's3cmd'


def psql_csv_run(sql_command, error_handler=None):
    """
    Runs psql and returns a CSVReader object from the query

    This CSVReader includes header names as the first record in all
    situations.  The output is fully buffered into Python.

    """
    csv_query = ('COPY ({query}) TO STDOUT WITH CSV HEADER;'
                 .format(query=sql_command))

    psql_proc = subprocess.Popen([PSQL_BIN, '-d', 'postgres', '-c', csv_query],
                                 stdout=subprocess.PIPE)
    stdout, stderr = psql_proc.communicate()

    if psql_proc.returncode != 0:
        if error_handler is not None:
            error_handler(psql_proc)
        else:
            assert error_handler is None
            raise Exception('Could not csv-execute "{query}" successfully'
                            .format(query=self._sqlcmd))

    # Previous code must raise any desired exceptions for non-zero
    # exit codes
    assert psql_proc.returncode == 0

    # Fake enough iterator interface to get a CSV Reader object
    # that works.
    return csv.reader(iter(stdout.strip().split('\n')))

class PgBackupStatements(object):
    """
    Contains operators to start and stop a backup on a Postgres server

    Relies on PsqlHelp for underlying mechanism.

    """

    @staticmethod
    def _dict_transform(csv_reader):
        rows = list(csv_reader)
        assert len(rows) == 2, 'Expect header row and data row'
        assert len(rows[1]) == 2, 'Expect (wal_file_name, offset) tuple'
        return dict(zip(*rows))

    @classmethod
    def run_start_backup(cls):
        """
        Connects to a server and attempts to start a hot backup

        Yields the WAL information in a dictionary for bookkeeping and
        recording.

        """
        def handler(popen):
            assert popen.returncode != 0
            raise Exception('Could not start hot backup')

        label = 'freeze_start_' + datetime.datetime.now().isoformat()

        return cls._dict_transform(psql_csv_run(
                "SELECT file_name, file_offset "
                "FROM pg_xlogfile_name_offset("
                "pg_start_backup('{0}'))".format(label),
                error_handler=handler))

    @classmethod
    def run_stop_backup(cls):
        """
        Stop a hot backup, if it was running, or error

        Return the last WAL file name and position that is required to
        gain consistency on the captured heap.

        """
        def handler(popen):
            assert popen.returncode != 0
            raise Exception('Could not stop hot backup')

        label = 'freeze_start_' + datetime.datetime.now().isoformat()

        return cls._dict_transform(psql_csv_run(
                "SELECT file_name, file_offset "
                "FROM pg_xlogfile_name_offset("
                "pg_stop_backup())", error_handler=handler))


def do_put(s3_url, path, s3cmd_config_path):
    """
    Synchronous version of the s3-upload wrapper

    Nominally intended to be used through a pool, but exposed here
    for testing and experimentation.

    """
    with tempfile.NamedTemporaryFile(mode='w') as tf:
        compression_p = subprocess.Popen(
            [LZOP_BIN, '--stdout', path], stdout=tf)
        compression_p.wait()

        if compression_p.returncode != 0:
            raise Exception(
                'Could not properly compress heap file: {path}'
                .format(path=path))

        # Not to be confused with fsync: the point is to make
        # sure any Python-buffered output is visible to other
        # processes, but *NOT* force a write to disk.
        tf.flush()

        subprocess.check_call([S3CMD_BIN, '-c', s3cmd_config_path,
                               'put', tf.name, s3_url])

    return None


class S3Backup(object):
    """
    A performs s3cmd uploads to copy a PostgreSQL cluster to S3.

    Note that this is also lzo compresses the files: thus, the number
    of pooled processes involves doing a full sequential scan of the
    uncompressed Postgres heap file that is pipelined into lzo. Once
    lzo is completely finished (necessary to have access to the file
    size) the file is sent to S3.

    TODO: Investigate an optimization to decouple the compression and
    upload steps to make sure that the most efficient possible use of
    pipelining of network and disk resources occurs.  Right now it
    possible to bounce back and forth between bottlenecking on reading
    from the database block device and subsequently the S3 sending
    steps should the processes be at the same stage of the upload
    pipeline: this can have a very negative impact on being able to
    make full use of system resources.

    Furthermore, it desirable to overflowing the page cache: having
    separate tunables for number of simultanious compression jobs
    (which occupy /tmp space and page cache) and number of uploads
    (which affect upload throughput) would help.

    """

    def __init__(self,
                 aws_access_key_id, aws_secret_access_key,
                 s3_url_prefix, pg_cluster_dir,
                 pool_size=6):
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.s3_url_prefix = s3_url_prefix
        self.pg_cluster_dir = pg_cluster_dir
        self.pool = multiprocessing.Pool(processes=pool_size)

    def upload_file(self, s3_url, path, s3cmd_config_path):
        """
        Asychronously uploads the path to the provided s3 url

        Returns a multiprocessing async result, which, when complete,
        will yield "None".  However, the result may also have an
        exception: this should be carefully checked for by callers to
        ensure the operation has (likely) succeeded.

        Mechanism includes lzo compression.  Because S3 requires a
        file size when starting the upload, it is necessary to buffer
        the complete compressed output in a temp file as to measure
        its size.  This is unfortunate but probably worth it because
        lzo output tends to be between 10% and 30% of the original
        heap file size.  Special effort should be made to not sync()
        to disk, so that most of the temp file mangling will occur
        in-memory in practice.

        """
        return self.pool.apply_async(do_put, [s3_url, path, s3cmd_config_path])

    def s3_upload_pg_cluster_dir(self):
        """
        Upload to s3_url_prefix from pg_cluster_dir

        This function ignores the directory pg_xlog, which contains WAL
        files and are not generally part of a base backup.

        """

        # Get a manifest of files first.
        matches = []

        def raise_walk_error(e):
            raise e

        walker = os.walk(self.pg_cluster_dir, onerror=raise_walk_error)
        for root, dirnames, filenames in walker:
            # Don't care about WAL, only heap. Also skip the textual log
            # directory.
            if 'pg_xlog' in dirnames:
                dirnames.remove('pg_xlog')

            for filename in filenames:
                matches.append(os.path.join(root, filename))

        canonical_s3_prefix = self.s3_url_prefix.rstrip('/')

        # absolute upload paths are used for telling lzop what to compress
        absolute_upload_paths = [os.path.abspath(match) for match in matches]

        # computed to subtract out extra extraneous absolute path
        # information when storing on S3
        common_local_prefix = os.path.commonprefix(absolute_upload_paths)

        with tempfile.NamedTemporaryFile(mode='w') as s3cmd_config:
            s3cmd_config.write(textwrap.dedent("""\
            [default]
            access_key = {aws_access_key_id}
            secret_key = {aws_secret_access_key}
            """).format(aws_access_key_id=self.aws_access_key_id,
                        aws_secret_access_key=self.aws_secret_access_key))

            s3cmd_config.flush()

            uploads = []
            for absolute_upload_path in absolute_upload_paths:
                remote_suffix = absolute_upload_path[len(common_local_prefix):]
                uploads.append(self.upload_file('/'.join(
                            [canonical_s3_prefix,  remote_suffix]),
                        absolute_upload_path, s3cmd_config.name))


            self.pool.close()

            got_sigint = False
            while uploads and not got_sigint:
                try:
                    if uploads:
                        # XXX: Need timeout to work around Python bug:
                        #
                        # http://bugs.python.org/issue8296
                        uploads.pop().get(1e100)
                        continue

                    self.pool.join()
                except KeyboardInterrupt:
                    got_sigint = True

    def database_s3_backup(self):
        """
        Wraps s3_upload_pg_cluster_dir with start/stop backup actions

        """

        upload_good = False
        backup_stop_good = False
        try:
            start_backup_info = PgBackupStatements.run_start_backup()
            self.s3_upload_pg_cluster_dir()
            upload_good = True
        finally:
            stop_backup_info = PgBackupStatements.run_stop_backup()
            backup_stop_good = True

        if not (upload_good and backup_stop_good):
            # NB: Other exceptions should be raised before this that
            # have more informative results, it is intended that this
            # exception never will get raised.
            raise Exception('Could not complete backup process')


def external_program_check():
    """
    Validates the existence and basic working-ness of other programs

    Implemented because it is easy to get confusing error output when
    one does not install a dependency because of the fork-worker model
    that is both necessary for throughput and makes more obscure the
    cause of failures.  This is intended to be a time and frustration
    saving measure.  This problem has confused The Author in practice
    when switching rapidly between machines.

    """

    could_not_run = []
    error_msgs = []

    def psql_err_handler(popen):
        assert popen.returncode != 0
        error_msgs.append(textwrap.fill(
                'Could not get a connection to the database: '
                'note that superuser access is required'))

        # Bogus error message that is re-caught and re-raised
        raise Exception('It is also possible that psql is not installed')

    with open(os.devnull, 'w') as nullf:
        for program in [PSQL_BIN, LZOP_BIN, S3CMD_BIN]:
            try:
                if program is PSQL_BIN:
                    psql_csv_run('SELECT 1', error_handler=psql_err_handler)
                else:
                    subprocess.call([program], stdout=nullf, stderr=nullf)
            except IOError, e:
                could_not_run.append(program)

    if could_not_run:
        error_msgs.append(textwrap.fill(
                'Could not run the following programs with exit '
                'status zero, are they installed and working? ' +
                ', '.join(could_not_run)))


    if error_msgs:
        raise Exception('\n' + '\n'.join(error_msgs))

    return None


def main(argv=None):
    if argv is None:
        argv = sys.argv

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)

    parser.add_argument('-k', '--aws-access-key-id',
                        help='public AWS access key. Can also be defined in an '
                        'environment variable. If both are defined, '
                        'the one defined in the programs arguments takes '
                        'precedence.')
    parser.add_argument('S3BASEURL',
                        help="base URL in s3 to upload to, "
                        "such as 's3://bucket/directory/'")
    parser.add_argument('PG_CLUSTER_DIRECTORY',
                        help="Postgres cluster path, "
                        "such as '/var/lib/database'")
    parser.add_argument('--pool-size', '-p',
                        type=int,
                        help='Upload pooling size')

    args = parser.parse_args()

    secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')
    if secret_key is None:
        print >>sys.stderr, ('Must define AWS_SECRET_ACCESS_KEY to upload '
                             'anything')
        sys.exit(1)

    if args.aws_access_key_id is None:
        aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
        if aws_access_key_id is None:
            print >>sys.stderr, ('Must define an AWS_ACCESS_KEY_ID, '
                                 'using environment variable or '
                                 '--aws_access_key_id')

    else:
        aws_access_key_id = args.aws_access_key_id

    external_program_check()

    backup = (S3Backup(aws_access_key_id, secret_key, args.S3BASEURL,
                       args.PG_CLUSTER_DIRECTORY, pool_size=args.pool_size)
              .database_s3_backup())

if __name__ == "__main__":
    sys.exit(main())
