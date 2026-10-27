package com.gamerec.library.repository;

import com.gamerec.library.entity.EntryStatus;
import com.gamerec.library.entity.LibraryEntry;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

/**
 * Spring Data writes the implementation. A method named findByXAndY becomes
 * "where x = ? and y = ?"; the two @Query methods select one column instead of whole entities.
 */
public interface LibraryEntryRepository extends JpaRepository<LibraryEntry, Long> {

    Optional<LibraryEntry> findByUserIdAndAppId(String userId, int appId);

    Page<LibraryEntry> findByUserId(String userId, Pageable pageable);

    Page<LibraryEntry> findByUserIdAndStatus(String userId, EntryStatus status, Pageable pageable);

    @Query("select e.appId from LibraryEntry e where e.userId = :userId order by e.appId")
    List<Integer> findAppIds(@Param("userId") String userId);

    @Query("select e.appId from LibraryEntry e"
            + " where e.userId = :userId and e.status = :status order by e.appId")
    List<Integer> findAppIdsByStatus(@Param("userId") String userId,
                                     @Param("status") EntryStatus status);
}
