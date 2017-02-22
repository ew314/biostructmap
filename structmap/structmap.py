"""
Main PDB Structure Mapping (StructMap) module. This package implements
techniques to map data onto a protein PDB structure.

Rationale:

Whilst many protein-related data (such as genetic polymorphisms) can be easily
related to a reference protein sequence, it is slightly more involved to map
this data onto a protein structure. For example, when viewing polymorphisms
that have arisen as a result of immune selection pressure, it is instructive
to be able to view the location of these polymorphisms in the context of the
protein 3D structure. Furthermore, it may also be instructive to average this
data over a radius around a central residue. In our example case of protein
polymoprhisms, this allows the user to identify polymorphic hotspots on the
protein structure.

We can extend this concept further, and consider genetic tests for phenomena
which arise as a result of selection pressure at the level of protein structure.
For example, antibody-mediated immune selection pressure gives rise to balancing
selection at the level of population genetics. A number of methods exist to
identify regions of the genome under balancing selection, including Tajima's D.
Tajima's D is often calculated as a sliding window over a genome. However, when
the origins of balancing selection occur at the level of protein structure,
it can be useful to also consider protein spatial information when computing
Tajima's D using a sliding window. In essence, we could calculate Tajima's D
using a 3D sliding window over a protein structure. The StructMap package
automates this process.

Details:

The StructMap package makes extensive use of the Biopython Bio.PDB module for
PDB file parsing and integration with calculation of secondary structure
and relative solvent accessibility via the DSSP program. Calculation of Tajima's
D is performed using DendroPy, although several optimisations are performed
before passing a multiple sequence alignment to DendroPy in order to speed
up calculation of Tajima's D, such as removal of non-polymorphic sites and
memoization of results from previous windows when calculating a sliding window
value of Tajima's D.


"""
from __future__ import absolute_import, division, print_function

from copy import deepcopy
import tempfile
from os import path
from Bio.PDB import DSSP, PDBIO, PDBParser, Select
from Bio import AlignIO
from . import utils, pdbtools, gentests
from .pdbtools import (_tajimas_d, _default_mapping, _snp_mapping,
                       _map_amino_acid_scale,
                       match_pdb_residue_num_to_seq,
                       ss_lookup_dict)
from .seqtools import (blast_sequences, align_protein_to_dna,
                       _construct_sub_align)

class DataMap(dict):
    '''
    A class to hold a mapping of data to some PDB object. Extends the `dict`
    class, adding a few methods to allow correspondence between data and PDB
    chain object.
    '''
    def __init__(self, *args, **kw):
        super(DataMap, self).__init__(*args, **kw)
        self.chain = kw['chain']
        self.params = kw['params']

    def write_data_to_pdb_b_factor(self, default_no_value=0, outdir='', filename=None):
        #Make copy of chain object
        _chain = deepcopy(self.chain)
        _structure = _chain.parent().parent()
        #Set all B-factor fields to zeros
        for residue in _chain:
            _data = self.get(residue.get_id()[1], default_no_value)
            for atom in residue:
                atom.set_bfactor(float(_data))
        io = PDBIO()
        io.set_structure(_chain.chain)
        if filename is None:
            filename = _structure.pdbname + '_' + self._parameter_string() + '.pdb'
        io.save(path.join(outdir, filename))
        return None

    def _parameter_string(self):
        param_string = '_'.join(["{k}-{v}".format(k=key, v=value)
                                 for key, value in sorted(self.params.items())])
        return param_string


class Structure(object):
    """A class to hold a PDB structure object."""
    def __init__(self, pdbfile, pdbname='pdb_file'):
        #create pdb parser, get structure.
        parser = PDBParser()
        #Get Bio.PDB structure
        self._pdbfile = pdbfile
        self.structure = parser.get_structure(pdbname, self.pdb_file())
        #Get PDB sequences
        self.sequences = pdbtools.get_pdb_seq(self.pdb_file())
        self.models = {model.get_id():Model(self, model) for
                       model in self.structure}

        self.pdbname = pdbname

    def __iter__(self):
        for key in sorted(self.models):
            yield self.models[key]

    def __getitem__(self, key):
        return self.models[key]

    def pdb_file(self):
        '''
        Return the PDB file object, which can either be a string, or a file-like
        object. If it is a file-like object, sets read point to start of the
        file or stream.
        '''
        if not isinstance(self._pdbfile, str):
            self._pdbfile.seek(0)
        return self._pdbfile

    def map(self, data, method='default', ref=None, radius=15, selector='all'):
        """Function which performs a mapping of some parameter or function to
        a pdb structure, with the ability to apply the function over a
        '3D sliding window'. The residues within a radius of a central
        residue are passed to the function, which computes an output value for
        the central residue. This is performed for each residue in the
        structure.
        """
        output = {}
        for model in self:
            for chain in model:
                unique_id = (model.get_id(), chain.get_id())
                output[unique_id] = chain.map(data, method=method, ref=ref,
                                              radius=radius, selector=selector)
        return output


class Model(object):
    """A class to hold a PDB model object. """
    def __init__(self, structure, model):
        self._id = model.get_id()
        self.model = model
        self._parent = structure
        #DSSP only works on the first model in the PDB file
        if isinstance(structure.pdb_file(), str) and self._id == 0:
            try:
                self.dssp = DSSP(self.model, structure.pdb_file())
            except OSError:
                self.dssp = DSSP(self.model, structure.pdb_file(), dssp="mkdssp")
        elif self._id == 0:
            with tempfile.NamedTemporaryFile(mode='w') as temp_pdb_file:
                temp_pdb_file.write(structure.pdb_file().read())
                temp_pdb_file.flush()
                try:
                    self.dssp = DSSP(self.model, temp_pdb_file.name)
                except OSError:
                    self.dssp = DSSP(self.model, temp_pdb_file.name, dssp="mkdssp")
        else:
            self.dssp = {}
        self.chains = {chain.get_id():Chain(self, chain) for
                       chain in self.model}

    def __iter__(self):
        for key in sorted(self.chains):
            yield self.chains[key]

    def __getitem__(self, key):
        return self.chains[key]

    def parent(self):
        """Get parent Structure object"""
        return self._parent

    def get_id(self):
        """Get model ID"""
        return self._id


class Chain(object):
    """A class to hold a PDB chain object."""
    def __init__(self, model, chain):
        self._id = chain.get_id()
        self.chain = chain
        self._parent = model
        #Refer to model for DSSP data
        self.dssp = self._parent.dssp
        #Some PDB files do not contain sequences in the header, and
        #hence we need to parse the atom records for each chain
        try:
            self.sequence = model.parent().sequences[self.get_id()]
        except KeyError:
            self.sequence = pdbtools.get_pdb_seq_from_atom(chain)
            self._parent.parent().sequences[self.get_id()] = self.sequence
        self._nearby = {}
        self._rsa = {}

    def __iter__(self):
        for residue in self.chain:
            yield residue

    def __getitem__(self, key):
        return self.chain[key]

    def parent(self):
        """Get parent Model object"""
        return self._parent

    def get_id(self):
        """Get chain ID"""
        return self._id

    def nearby(self, radius=15, atom='all'):
        """Take a Bio.PDB chain object, and find all residues within a radius of
        a given residue. Return a dictionary containing nearby residues for each
        residue in the chain.
        Optional parameter is the atom with which to compute distance.
        By default this is 'all', which gets all non-heterologous atoms.
        Other potential options include 'CA', 'CB' etc. If an atom is not found
        within a residue object, then method reverts to using 'CA'.
        """
        param_key = (radius, atom)
        #Calculate distance matrix and store it for retrieval in future queries.
        if param_key not in self._nearby:
            dist_map = pdbtools.nearby(self.chain, radius, atom)
            self._nearby[param_key] = dist_map
        return self._nearby[param_key]

    def rel_solvent_access(self):
        """Use Bio.PDB to calculate relative solvent accessibility.
        Return a dictionary with RSA values for each residue.
        """
        if not self._rsa:
            rsa = {}
            for residue in self.chain:
                key = (self.get_id(), residue.get_id())
                if key in self.dssp:
                    try:
                        rsa[key[1][1]] = float(self.dssp[key][3])
                    except ValueError:
                        rsa[key[1][1]] = None
            self._rsa = rsa
        return self._rsa

    def secondary_structure(self, numeric_ss_code=False):
        '''Use DSSP to calculate secondary structure elements.
        Return a dictionary with secondary structure assignment (value)
        for each residue (key) within the chain.
        Secondary structure assignment notation follows that of the DSSP program.
        The DSSP codes for secondary structure used here are:
            =====     ====
            Code      Structure
            =====     ====
             H        Alpha helix (4-12)
             B        Isolated beta-bridge residue
             E        Strand
             G        3-10 helix
             I        Pi helix
             T        Turn
             S        Bend
             -       None
            =====     ====
        If kwarg `numeric_ss_code` == True, then secondary structure values
        will be returned as integers according to the following lookup table:
        {
        'H': 0,
        'B': 1,
        'E': 2,
        'G': 3,
        'I': 4,
        'T': 5,
        'S': 6,
        '-', 7
        }
        These are provided in pdbtools.ss_lookup_dict, as well as the reverse
        lookups (i.e. 0: 'H').
        '''
        keys = [x for x in self.dssp.keys() if x[0] == self._id]
        ss_dict = {x[1][1]: self.dssp.property_dict[x][2] for x in keys}
        if numeric_ss_code:
            return {key:ss_lookup_dict[item] for key, item in ss_dict.items()}
        else:
            return ss_dict

    def map(self, data, method='default', ref=None, radius=15, selector='all'):
        """Perform a mapping of some parameter or function to a pdb structure,
        with the ability to apply the function over a '3D sliding window'.
        The residues within a radius of a central residue are passed to the
        function, which computes an output value for the central residue.
        This is performed for each residue in the structure.
        For tajimasd, reference sequence is a genomic sequence.
        """
        #Note: This method attempts to deal with 3 different ways of identifying
        #residue position: i) Within a PDB file, residues are labelled with a
        #number, which should increment according to position in sequence, but
        #is not necessarily gapless, and may include negative numbering in
        #order to preserve alignment with similar sequences. ii) Residue
        #numbering according to the position of the residue within the
        #protein sequence extracted from the PDB file. That is, the first
        #residue in the sequence is numbering '1', and incrementing from there.
        #iii) Residue numbering according to a reference sequence provided, or
        #according to a dna sequence (which could include introns).

        methods = {"default":_default_mapping,
                   "tajimasd":_tajimas_d,
                   "snps": _snp_mapping,
                   "aa_scale": _map_amino_acid_scale}
        #Create a map of pdb sequence index (1-indexed) to pdb residue
        #numbering from file
        seq_index_to_pdb_numb = match_pdb_residue_num_to_seq(self, self.sequence)
        #Generate a map of nearby residues for each residue in pdb file. numbering
        #is according to pdb residue numbering from file
        residue_map = self.nearby(radius=radius, atom=selector)
        results = {}
        if ref is None and method != 'tajimasd':
            ref = self.sequence
        elif ref is None and method == 'tajimasd':
            ref = data[0]
        #Generate mapping of pdb sequence index to dna sequence
        if method == 'tajimasd':
            pdbindex_to_ref = align_protein_to_dna(self.sequence,
                                                   ''.join([x for x in ref]))
        #Generate mapping of pdb sequence index to reference sequence (also indexed by position)
        else:
            pdbindex_to_ref, _ = blast_sequences(self.sequence, ref)
        if method in methods:
            method = methods[method]
        #Finally, map pdb numbering by file to the reference sequence
        #(dna or protein) provided, as long as the residues exists within the PDB
        #structure (ie has coordinates)
        pdbnum_to_ref = {seq_index_to_pdb_numb[x]:pdbindex_to_ref[x] for x in
                         pdbindex_to_ref if x in seq_index_to_pdb_numb}
        results = {}
        #For each residue within the sequence, apply a function and return result.
        for residue in residue_map:
            results[residue] = pdbtools.map_function(self, method, data,
                                                     residue_map[residue],
                                                     ref=pdbnum_to_ref)
        params = {'radius':radius, 'selector': selector}
        return DataMap(results, chain=self, params=params)

    def write_to_atom(self, data, output, sep=','):
        """Write score for each atom in a structure to a file, based on
        a data dictionary mapping output score to residue number.
        """
        #For each atom in the structure, write an output score based on the data
        #given, presuming (key,value) in a dictionary with key corresponding to
        #a residue number.
        with open(output, 'w') as f:
            for res in data:
                residue = self.chain[int(res)]
                for atom in residue:
                    data_pt = [str(x) for x in [atom.serial_number, data[res]]]
                    line = sep.join(data_pt) + '\n'
                    f.write(line)

    def residue_to_atom_map(self):
        """Return a map of residue number (per numbering in PDB file)
        to atom number for this chain
        """
        mapping = {residue.id[1]:tuple(atom.serial_number for atom in residue) for residue in self.chain}
        return mapping

    def write_to_residue(self, data, output, sep=',', ref=None):
        """Write score for each residue in a structure to a file, based on a
        dictionary mapping output score to residue number.
        """
        if ref is None:
            with open(output, 'w') as f:
                for res in data:
                    data_pt = [str(x) for x in [res, data[res]]]
                    line = sep.join(data_pt) + '\n'
                    f.write(line)
        else:
            seq_index_to_pdb_numb = match_pdb_residue_num_to_seq(self, self.sequence)
            pdbindex_to_ref, _ = blast_sequences(self.sequence, ref)
            pdbnum_to_ref = {seq_index_to_pdb_numb[x]:pdbindex_to_ref[x] for x
                             in pdbindex_to_ref if x in seq_index_to_pdb_numb}
            with open(output, 'w') as f:
                for res in data:
                    if res not in pdbnum_to_ref:
                        output = ("Residue {res} in PDB file {pdb} was not"
                                  " matched to reference sequence provided"
                                  " for writing to output file").format(
                                        res=res, pdb=self.parent().parent().pdbname)

                        print(output)
                        continue
                    data_pt = [str(x) for x in [pdbnum_to_ref[res], data[res]]]
                    line = sep.join(data_pt) + '\n'
                    f.write(line)


class SequenceAlignment(object):
    """A class to hold a multiple sequence alignment object.
    Methods are:
        translate - translate to a protein sequence
        tajimas_d - calculate Tajima's D over a sliding window, or for the
        entire sequence.
    """
    def __init__(self, alignfile, file_format='fasta'):
        self.alignment = AlignIO.read(alignfile, file_format)
        self.alignment_fasta = self.alignment.format('fasta')
        self._alignment_position_dict = None
        self._isolate_ids = None

    def __getitem__(self, key):
        return self.alignment[key]

    def get_alignment_position_dict(self):
        if self._alignment_position_dict is None:
            self._alignment_position_dict = {}
            for i in range(len(self.alignment[0])):
                self._alignment_position_dict[i] = self.alignment[:, i]

        return self._alignment_position_dict

    def get_isolate_ids(self):
        if self._isolate_ids is None:
            self._isolate_ids = [seq.id for seq in self.alignment]
        return self._isolate_ids


    def tajimas_d(self, window=None, step=3, protein_ref=None, genome_ref=None,
                  output_protein_num=False):
        """Calculate Tajima's D on a SequenceAlignment object.

        If no window parameter is passed to the function, then the function
        calculates Tajima's D over the whole sequence alignment and
        returns a single numerical result.

        If a window size is given, then the function returns a dictionary
        of Tajima's D values over a sliding window.

        :param window: The size of the sliding window over which Tajima's D is
        calculated
        :type window: int
        :param step: Step size for sliding window calculation
        :type step: int
        :returns: *key: window midpoint
                  *value: Tajima's D value for window
        """
        # If given a protein reference sequence, align genome to reference
        # sequence, and only perform a Tajima's D analysis over the protein
        # coding region. Otherwise perform a Tajima's D test over the full
        # genomic sequence.
        if genome_ref is not None and protein_ref is not None:
            #Align genome to reference
            prot_to_genome = align_protein_to_dna(protein_ref, ''.join([x for x in genome_ref]))
            #Get sorted list of codons
            codons = [prot_to_genome[x] for x in sorted(prot_to_genome)]
            #Construct a sub-alignment
            alignment = _construct_sub_align(self, codons, fasta=True)
        elif genome_ref is None and protein_ref is None:
            alignment = self.alignment
        elif protein_ref is None:
            raise TypeError("Missing protein_ref assignment")
        elif genome_ref is None:
            raise TypeError("Missing genome_ref assignment")

        #Perform Tajima's D calculation
        try:
            tajd = gentests.tajimas_d(alignment, window, step)
        except TypeError:
            print("Error calculating Tajima's D. Please check inputs to " +
                  "Tajima's D function.")
            raise
        if window is not None and protein_ref is not None:
            #Unpack list of codons
            codon_list = [x for sublist in codons for x in sublist]
            #Reference to genome numbering
            tajd_ref_to_genome = {codon_list[int(x) - 1]: tajd[x] for x in tajd}
            #Dictionary comprehension to reverse and unpack map of protein
            #position to genome codons. Initial dictionary is in the form
            # {1:(1,2,3), 2:(4,5,6,),...}, hence the need for the multiple
            #layers within the dictionary comprehension (needed to unpack each
            #codon tuple).
            genome_to_prot = {i:x for x in prot_to_genome for i in prot_to_genome[x]}
            tajd_ref_to_protein = [(genome_to_prot[x], tajd_ref_to_genome[x])
                                   for x in tajd_ref_to_genome]
            if output_protein_num:
                return tajd_ref_to_genome, tajd_ref_to_protein
            else:
                return tajd_ref_to_genome
        else:
            return tajd
